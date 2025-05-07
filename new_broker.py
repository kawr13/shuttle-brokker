import asyncio
import os
import logging
import time
from typing import Dict, Optional, Tuple, List, Any
from contextlib import asynccontextmanager
from enum import Enum
import heapq  # Для Priority Queue элементов (кортежей)

from fastapi import FastAPI, HTTPException, Path, Body
from pydantic import BaseModel, Field
from shuttle_conf import SHUTTLE_CONFIG
from dotenv import load_dotenv

load_dotenv()

# --- Константы и Конфигурация ---

# IP и порт, на котором шлюз слушает статусы от шаттлов
WMS_LISTEN_IP = os.getenv('WMS_LISTEN_IP', '0.0.0.0')
WMS_LISTEN_PORT = os.getenv('WMS_LISTEN_PORT', 8000)

# Конфигурация шаттлов

MESSAGE_TERMINATOR = b'\n'
ENCODING = os.getenv('ENCODING', 'utf-8')
COMMAND_CONNECTION_TIMEOUT = float(os.getenv('COMMAND_CONNECTION_TIMEOUT', 5.0))  # 10 секунд
STATUS_READ_TIMEOUT = float(os.getenv('STATUS_READ_TIMEOUT', 300.0)) # Таймаут чтения статуса от шаттла (5 минут)
COMMAND_COMPLETION_TIMEOUT = float(os.getenv('COMMAND_COMPLETION_TIMEOUT', 180.0))  # Таймаут ожидания завершения команды (3 минуты)

# Максимальное количество попыток переподключения к командному порту
MAX_COMMAND_CONNECTION_ATTEMPTS = int(os.getenv('MAX_COMMAND_CONNECTION_ATTEMPTS', 5))
# Начальная задержка перед повторной попыткой подключения (секунды)
INITIAL_RETRY_DELAY = float(os.getenv('INITIAL_RETRY_DELAY', 1.0))

# Префиксы сообщений от шаттла
F_CODE_PREFIX = os.getenv('F_CODE_PREFIX', "F_CODE=")
LOCATION_PREFIX = os.getenv('LOCATION_PREFIX', "LOCATION=")  # Для LOCATION=XXXXX
COUNT_PREFIX = os.getenv('COUNT_PREFIX', "COUNT_")  # Для COUNT_XXXXXXXXXX=NNN
STATUS_PREFIX = os.getenv('STATUS_PREFIX', "STATUS=")
BATTERY_PREFIX = os.getenv('BATTERY_PREFIX', "BATTERY=")
WDH_PREFIX = os.getenv('WDH_PREFIX', "WDH=")
WLH_PREFIX = os.getenv('WLH_PREFIX', "WLH=")

# Настройка логирования
logging.basicConfig(
    level=logging.INFO,
    format='%(asctime)s - %(levelname)s - [%(name)s] - %(message)s',
    datefmt='%Y-%m-%d %H:%M:%S'
)
logger = logging.getLogger("WMS_Gateway")


# --- Модели данных ---

class CommandPriority(int, Enum):
    HIGH = 1  # Для критических команд, например, HOME
    MEDIUM = 2  # Для стандартных операций
    LOW = 3  # Для запросов статуса и некритичных команд


class CommandPayload(BaseModel):
    command: str = Field(..., description="Команда для шаттла, например, 'PALLET_IN' или 'FIFO-003'")
    repeat: int = Field(1, ge=1, description="Количество повторений команды")


# --- Исключения ---

class ShuttleConnectionError(Exception):
    """Ошибка соединения с шаттлом."""
    pass


class ShuttleCommandError(Exception):
    """Ошибка выполнения команды шаттлом."""
    pass


# --- Класс Шаттла ---

class Shuttle:
    def __init__(self, shuttle_id: str, ip: str, command_port: int, name: str):
        self.shuttle_id = shuttle_id
        self.ip = ip
        self.command_port = command_port
        self.name = name
        self.logger = logging.getLogger(f"Shuttle.{self.name.replace(' ', '_')}")

        self._command_reader: Optional[asyncio.StreamReader] = None
        self._command_writer: Optional[asyncio.StreamWriter] = None
        self._command_lock = asyncio.Lock()
        self._command_connection_attempts = 0

        self._status_reader: Optional[asyncio.StreamReader] = None
        self._status_writer: Optional[asyncio.StreamWriter] = None
        self._status_lock = asyncio.Lock()
        self._status_handler_task: Optional[asyncio.Task] = None

        self.last_status: Optional[str] = None
        self.last_f_code: Optional[str] = None
        self.is_status_connected: bool = False
        self.is_operational: bool = True  # Готовность шаттла к работе

        # Очередь команд: (priority, timestamp, command_str, repeat_count)
        self._command_queue: asyncio.PriorityQueue[Tuple[int, float, str, int]] = asyncio.PriorityQueue()
        self._command_processor_task: Optional[asyncio.Task] = None

        self.logger.info(f"Инициализирован. IP: {self.ip}:{self.command_port}")

    def _get_priority_for_command(self, command_str: str) -> CommandPriority:
        base_command = command_str.split('-')[0].upper()
        if base_command == "HOME":
            return CommandPriority.HIGH
        if base_command in ["STATUS", "BATTERY", "WDH", "WLH"]:
            return CommandPriority.LOW
        return CommandPriority.MEDIUM

    async def _ensure_command_connection(self):
        if self._command_writer and not self._command_writer.is_closing():
            # Проверим соединение, отправив пустое сообщение (или keep-alive, если поддерживается)
            # Для простоты, предполагаем, что если writer есть, он рабочий.
            # Более надежная проверка может включать отправку пинг-сообщения.
            return

        async with self._command_lock:
            if self._command_writer and not self._command_writer.is_closing():  # Двойная проверка на случай конкуренции
                return

            self.logger.info("Попытка установить командное соединение...")
            retry_delay = INITIAL_RETRY_DELAY
            for attempt in range(MAX_COMMAND_CONNECTION_ATTEMPTS):
                try:
                    self._command_reader, self._command_writer = await asyncio.wait_for(
                        asyncio.open_connection(self.ip, self.command_port),
                        timeout=COMMAND_CONNECTION_TIMEOUT
                    )
                    self.logger.info(f"Командное соединение установлено: {self.ip}:{self.command_port}")
                    self._command_connection_attempts = 0  # Сброс счетчика попыток
                    return
                except Exception as e:
                    self.logger.warning(
                        f"Попытка {attempt + 1}/{MAX_COMMAND_CONNECTION_ATTEMPTS} подключения к командному порту не удалась: {e}")
                    if attempt < MAX_COMMAND_CONNECTION_ATTEMPTS - 1:
                        await asyncio.sleep(retry_delay)
                        retry_delay *= 2  # Exponential backoff
                    else:
                        self.logger.error("Не удалось установить командное соединение после нескольких попыток.")
                        self._command_reader = None
                        self._command_writer = None
                        raise ShuttleConnectionError(f"Не удалось подключиться к {self.ip}:{self.command_port}: {e}")

    async def _send_single_command(self, command: str) -> bool:
        """Отправляет одну команду шаттлу."""
        try:
            await self._ensure_command_connection()
            if not self._command_writer or self._command_writer.is_closing():
                self.logger.error("Командный writer недоступен.")
                raise ShuttleConnectionError("Командный writer недоступен.")

            full_message = command.encode(ENCODING) + MESSAGE_TERMINATOR
            self._command_writer.write(full_message)
            await self._command_writer.drain()
            self.logger.info(f"Команда '{command}' успешно отправлена.")
            return True
        except ShuttleConnectionError:  # Уже залогировано в _ensure_command_connection
            await self._close_command_connection_internal()
            return False
        except Exception as e:
            self.logger.error(f"Ошибка при отправке команды '{command}': {e}")
            await self._close_command_connection_internal()
            return False

    async def _process_command_queue(self):
        self.logger.info("Запущен обработчик очереди команд.")
        while True:
            try:
                priority_val, timestamp, command_str, repeats_left = await self._command_queue.get()

                self.logger.info(
                    f"Извлечена команда из очереди: '{command_str}' (Приоритет: {priority_val}, Повторов осталось: {repeats_left})")

                if not self.is_operational and command_str.upper() != "HOME":
                    self.logger.warning(
                        f"Шаттл не в рабочем состоянии. Команда '{command_str}' не будет отправлена (кроме HOME).")
                    self._command_queue.task_done()
                    continue

                success = await self._send_single_command(command_str)
                if not success:
                    self.logger.warning(f"Не удалось отправить команду '{command_str}'. Возвращаем в очередь.")
                    # Возвращаем в очередь с теми же параметрами, возможно, увеличить timestamp для сохранения порядка
                    await self._command_queue.put((priority_val, time.monotonic(), command_str, repeats_left))
                    self._command_queue.task_done()
                    await asyncio.sleep(INITIAL_RETRY_DELAY)  # Пауза перед следующей попыткой из очереди
                    continue

                await self._wait_for_completion(command_str)

                if repeats_left > 1:
                    self.logger.info(f"Команда '{command_str}' будет повторена. Осталось {repeats_left - 1} раз.")
                    await self._command_queue.put((priority_val, time.monotonic(), command_str, repeats_left - 1))

                self._command_queue.task_done()

            except ShuttleCommandError as e:  # Ошибка от _wait_for_completion (F_CODE или таймаут)
                self.logger.error(f"Ошибка выполнения команды '{command_str}': {e}")
                # Решаем, нужно ли повторять или отменять команду
                self._command_queue.task_done()  # Важно завершить задачу в очереди
            except asyncio.CancelledError:
                self.logger.info("Обработчик очереди команд остановлен.")
                return
            except Exception as e:
                self.logger.exception(f"Непредвиденная ошибка в обработчике очереди команд: {e}")
                # Возможно, стоит сделать паузу перед продолжением
                await asyncio.sleep(5)

    async def _wait_for_completion(self, command: str):
        base_cmd_parts = command.upper().split('-')
        base_cmd = base_cmd_parts[0]

        # Сообщения, свидетельствующие о завершении или ошибке команды
        completion_msgs = []
        if base_cmd == "PALLET_IN":
            completion_msgs = ["PALLET_IN_DONE", "PALLET_IN_ABORT"]
        elif base_cmd == "PALLET_OUT":
            completion_msgs = ["PALLET_OUT_DONE", "PALLET_OUT_ABORT"]
        elif base_cmd == "FIFO":
            completion_msgs = [f"{command.upper()}_DONE", f"{command.upper()}_ABORT"]
        elif base_cmd == "FILO":
            completion_msgs = [f"{command.upper()}_DONE", f"{command.upper()}_ABORT"]
        elif base_cmd == "STACK_IN":
            completion_msgs = ["STACK_IN_DONE", "STACK_IN_ABORT"]
        elif base_cmd == "STACK_OUT":
            completion_msgs = ["STACK_OUT_DONE", "STACK_OUT_ABORT"]
        elif base_cmd == "HOME":
            completion_msgs = [LOCATION_PREFIX, "HOME_ABORT"]  # LOCATION=... или LOCATION=NONE
        elif base_cmd == "COUNT":
            completion_msgs = [COUNT_PREFIX, "COUNT_ABORT"]  # COUNT_XXXXXXXXXX=NNN
        elif base_cmd == "STATUS":
            completion_msgs = [STATUS_PREFIX]
        elif base_cmd == "BATTERY":
            completion_msgs = [BATTERY_PREFIX]
        elif base_cmd == "WDH":
            completion_msgs = [WDH_PREFIX]
        elif base_cmd == "WLH":
            completion_msgs = [WLH_PREFIX]

        # Все операционные команды могут завершиться ошибкой F_CODE
        if base_cmd not in ["STATUS", "BATTERY", "WDH",
                            "WLH"]:  # Команды-запросы не должны генерировать F_CODE как ответ
            completion_msgs.append(F_CODE_PREFIX)

        start_time = asyncio.get_event_loop().time()
        self.logger.debug(f"Ожидание завершения команды '{command}'. Ожидаемые сообщения: {completion_msgs}")

        while asyncio.get_event_loop().time() - start_time < COMMAND_COMPLETION_TIMEOUT:
            current_status = self.last_status
            if current_status:
                # Сбрасываем last_status после обработки, чтобы не реагировать на него повторно
                # self.last_status = None # Осторожно: это может помешать другим частям системы видеть последний статус
                # Лучше проверять по времени получения статуса или использовать event.

                for msg_prefix in completion_msgs:
                    if current_status.startswith(msg_prefix):
                        self.logger.info(f"Команда '{command}' завершена. Статус: {current_status}")
                        if current_status.startswith(F_CODE_PREFIX):
                            raise ShuttleCommandError(f"Команда '{command}' завершилась с ошибкой: {current_status}")
                        return  # Успешное завершение или ожидаемый ABORT

            await asyncio.sleep(0.1)  # Проверка каждые 100 мс

        self.logger.warning(f"Таймаут ожидания завершения команды '{command}'.")
        raise ShuttleCommandError(f"Таймаут для команды '{command}'")

    async def queue_command(self, command_str: str, repeat: int = 1):
        priority = self._get_priority_for_command(command_str)
        # Добавляем первый экземпляр команды (или единственный, если repeat=1)
        # Остальные повторы (если repeat > 1) будут добавлены в _process_command_queue после выполнения предыдущего
        await self._command_queue.put((priority.value, time.monotonic(), command_str, repeat))

        self.logger.info(
            f"Команда '{command_str}' (Приоритет: {priority.name}) добавлена в очередь {repeat} раз(а). Размер очереди: {self._command_queue.qsize()}")

        if self._command_processor_task is None or self._command_processor_task.done():
            self.logger.info("Запускаем новый обработчик очереди команд.")
            self._command_processor_task = asyncio.create_task(self._process_command_queue())
        elif self._command_processor_task.cancelled():
            self.logger.warning("Обработчик очереди был отменен. Запускаем новый.")
            self._command_processor_task = asyncio.create_task(self._process_command_queue())

    async def _handle_f_code(self, f_code_message: str):
        self.last_f_code = f_code_message
        self.logger.error(f"Получен F_CODE: {f_code_message}")

        # Пример реакции на конкретные F_CODE
        if f_code_message == "F_CODE=1":  # Разряд аккумулятора
            self.logger.critical("НИЗКИЙ ЗАРЯД АККУМУЛЯТОРА! Шаттл требует зарядки/возврата на базу.")
            self.is_operational = False
        elif f_code_message in ["F_CODE=8", "F_CODE=9"]:  # Шаттл не в канале или не готов
            self.is_operational = False
        # Другие F_CODE могут требовать специфической обработки или просто логирования

    async def _handle_incoming_status(self):
        if not self._status_reader or not self._status_writer:
            self.logger.error("Отсутствует reader/writer для обработки статусов.")
            self.is_status_connected = False
            return

        peername = self._status_writer.get_extra_info('peername', ('?', 0))
        self.logger.info(f"Начата обработка статусов от {peername[0]}:{peername[1]}")
        self.is_status_connected = True
        try:
            while True:
                line_bytes = await asyncio.wait_for(self._status_reader.readline(), timeout=STATUS_READ_TIMEOUT)
                if not line_bytes:
                    self.logger.warning(f"Соединение для статусов закрыто со стороны {peername[0]}:{peername[1]}.")
                    break

                message = line_bytes.strip().decode(ENCODING, errors='ignore')
                self.logger.info(f"Получен статус: {message}")
                self.last_status = message  # Обновляем последний полученный статус

                if message.startswith(F_CODE_PREFIX):
                    await self._handle_f_code(message)

                # Отправляем MRCD (Message Received) в ответ на любое сообщение, кроме самого MRCD
                if message != "MRCD":
                    await self._send_mrcd()

        except asyncio.TimeoutError:
            self.logger.warning(f"Таймаут чтения статуса от {peername[0]}:{peername[1]}.")
        except asyncio.CancelledError:
            self.logger.info(f"Обработчик статусов для {peername[0]}:{peername[1]} остановлен.")
            # Не нужно вызывать close_status_connection здесь, т.к. cancel может быть извне
        except Exception as e:
            self.logger.error(f"Ошибка в обработчике статусов от {peername[0]}:{peername[1]}: {e}")
        finally:
            self.is_status_connected = False
            # Не закрываем соединение здесь, т.к. задача могла быть отменена.
            # Соединение закроется в close_status_connection или при новом set_status_connection.
            self.logger.info(f"Завершена обработка статусов от {peername[0]}:{peername[1]}.")

    async def _send_mrcd(self):
        async with self._status_lock:  # Гарантируем, что writer не изменится во время отправки
            if self._status_writer and not self._status_writer.is_closing():
                try:
                    mrcd_message = "MRCD".encode(ENCODING) + MESSAGE_TERMINATOR
                    self._status_writer.write(mrcd_message)
                    await self._status_writer.drain()
                    self.logger.debug("MRCD отправлен.")
                except Exception as e:
                    self.logger.error(f"Ошибка при отправке MRCD: {e}")
                    # Если отправка MRCD не удалась, возможно, статусное соединение разорвано
                    # Не вызываем close_status_connection здесь, чтобы избежать рекурсии или deadlock
            else:
                self.logger.warning("Попытка отправить MRCD, но статусное соединение отсутствует или закрывается.")

    async def set_status_connection(self, reader: asyncio.StreamReader, writer: asyncio.StreamWriter):
        async with self._status_lock:
            peername = writer.get_extra_info('peername', ('N/A', 0))
            self.logger.info(f"Установка нового статусного соединения от {peername[0]}:{peername[1]}.")

            # Закрываем предыдущее соединение и задачу, если они существуют
            if self._status_handler_task and not self._status_handler_task.done():
                self.logger.debug("Отменяем предыдущий обработчик статусов.")
                self._status_handler_task.cancel()
                try:
                    await self._status_handler_task
                except asyncio.CancelledError:
                    pass  # Ожидаемо
            if self._status_writer and not self._status_writer.is_closing():
                self.logger.debug("Закрываем предыдущий статусный writer.")
                self._status_writer.close()
                try:
                    await self._status_writer.wait_closed()
                except Exception:
                    pass  # Игнорируем ошибки при закрытии старого

            self._status_reader = reader
            self._status_writer = writer
            self.is_status_connected = True  # Устанавливаем флаг до запуска задачи

            # Сбрасываем статус при новом подключении, чтобы избежать реакции на старые данные
            self.last_status = None
            self.last_f_code = None
            # self.is_operational = True # Можно сбросить в True или оставить как есть, в зависимости от логики

            self._status_handler_task = asyncio.create_task(self._handle_incoming_status())
            self.logger.info(f"Новый обработчик статусов запущен для {peername[0]}:{peername[1]}.")

    async def _close_command_connection_internal(self):
        """Внутренний метод для закрытия командного соединения без блокировки _command_lock."""
        if self._command_writer:
            writer = self._command_writer
            self._command_reader = None
            self._command_writer = None  # Сначала обнуляем, чтобы предотвратить использование
            if not writer.is_closing():
                self.logger.info("Закрытие командного соединения.")
                try:
                    writer.close()
                    await writer.wait_closed()
                except Exception as e:
                    self.logger.error(f"Ошибка при закрытии командного соединения: {e}")
            else:
                self.logger.debug("Командное соединение уже закрывается/закрыто.")
        else:
            self.logger.debug("Командное соединение отсутствует, закрывать нечего.")

    async def close_all_connections(self):
        self.logger.info("Закрытие всех соединений и остановка задач...")
        # Остановка обработчика команд
        if self._command_processor_task and not self._command_processor_task.done():
            self._command_processor_task.cancel()
            try:
                await self._command_processor_task
            except asyncio.CancelledError:
                self.logger.debug("Обработчик команд отменен.")

        # Закрытие командного соединения
        async with self._command_lock:
            await self._close_command_connection_internal()

        # Закрытие статусного соединения
        async with self._status_lock:
            if self._status_handler_task and not self._status_handler_task.done():
                self._status_handler_task.cancel()
                try:
                    await self._status_handler_task
                except asyncio.CancelledError:
                    self.logger.debug("Обработчик статусов отменен.")

            if self._status_writer and not self._status_writer.is_closing():
                sw = self._status_writer
                self._status_reader = None
                self._status_writer = None  # Обнуляем
                self.is_status_connected = False
                self.logger.info("Закрытие статусного соединения.")
                try:
                    sw.close()
                    await sw.wait_closed()
                except Exception as e:
                    self.logger.error(f"Ошибка при закрытии статусного соединения: {e}")
        self.logger.info("Все соединения закрыты, задачи остановлены.")


# --- Глобальное состояние ---

shuttles: Dict[str, Shuttle] = {}
tcp_server: Optional[asyncio.AbstractServer] = None


# --- TCP Сервер для статусов от шаттлов ---

async def handle_shuttle_status_connection(reader: asyncio.StreamReader, writer: asyncio.StreamWriter):
    peername = writer.get_extra_info('peername', ('Неизвестный IP', 0))
    peer_ip = peername[0]
    logger.info(f"Входящее статусное соединение от {peer_ip}:{peername[1]}")

    found_shuttle: Optional[Shuttle] = None
    for shuttle_obj in shuttles.values():
        if shuttle_obj.ip == peer_ip:
            found_shuttle = shuttle_obj
            break

    if found_shuttle:
        await found_shuttle.set_status_connection(reader, writer)
    else:
        logger.warning(f"Неизвестный шаттл подключился с IP {peer_ip}. Закрытие соединения.")
        writer.close()
        try:
            await writer.wait_closed()
        except Exception:
            pass


# --- FastAPI Приложение ---

@asynccontextmanager
async def lifespan(app: FastAPI):
    global tcp_server, shuttles
    logger.info("Запуск шлюза WMS-Шаттл...")

    for shuttle_id_key, config in SHUTTLE_CONFIG.items():
        shuttles[shuttle_id_key] = Shuttle(
            shuttle_id=shuttle_id_key,
            ip=config["ip"],
            command_port=config["command_port"],
            name=config["name"]
        )

    try:
        tcp_server = await asyncio.start_server(
            handle_shuttle_status_connection,
            WMS_LISTEN_IP,
            WMS_LISTEN_PORT
        )
        addr = tcp_server.sockets[0].getsockname() if tcp_server.sockets else ('N/A', 0)
        logger.info(f"TCP сервер для статусов слушает на {addr[0]}:{addr[1]}")
    except Exception as e:
        logger.exception(f"Не удалось запустить TCP сервер для статусов: {e}")
        # Если сервер не запустился, дальнейшая работа бессмысленна
        # Здесь можно добавить логику аварийного завершения или повторных попыток
        raise RuntimeError(f"Не удалось запустить TCP сервер: {e}") from e

    yield  # Приложение работает

    logger.info("Остановка шлюза WMS-Шаттл...")
    if tcp_server:
        tcp_server.close()
        await tcp_server.wait_closed()
        logger.info("TCP сервер для статусов остановлен.")

    for shuttle_obj in shuttles.values():
        await shuttle_obj.close_all_connections()

    shuttles.clear()
    logger.info("Шлюз успешно остановлен.")


app = FastAPI(
    title="WMS-Shuttle Gateway",
    description="Шлюз для управления шаттлами и получения их статусов.",
    version="1.1.0",
    lifespan=lifespan
)


def get_shuttle_or_404(shuttle_id: str) -> Shuttle:
    shuttle = shuttles.get(shuttle_id)
    if not shuttle:
        logger.warning(f"Запрос к несуществующему шаттлу: {shuttle_id}")
        raise HTTPException(status_code=404, detail=f"Шаттл '{shuttle_id}' не найден.")
    return shuttle


# --- API Эндпоинты ---

@app.post("/shuttle/{shuttle_id}/command", status_code=202, summary="Отправить команду шаттлу")
async def send_command_to_shuttle(
        shuttle_id: str = Path(..., description="Идентификатор шаттла (например, 'shuttle1')"),
        payload: CommandPayload = Body(...)
):
    """
    Отправляет команду указанному шаттлу. Команда добавляется в очередь с приоритетом.
    """
    shuttle = get_shuttle_or_404(shuttle_id)

    # Проверка на валидность команды (базовая)
    if not payload.command or len(payload.command) > 100:  # Примерная проверка
        raise HTTPException(status_code=400, detail="Некорректная команда.")

    try:
        await shuttle.queue_command(payload.command, payload.repeat)
        return {
            "message": f"Команда '{payload.command}' добавлена в очередь для '{shuttle.name}' {payload.repeat} раз(а)."}
    except Exception as e:
        shuttle.logger.exception(f"Ошибка при добавлении команды '{payload.command}' в очередь.")
        raise HTTPException(status_code=500, detail=f"Внутренняя ошибка сервера: {e}")


@app.get("/shuttle/{shuttle_id}/status", summary="Получить статус шаттла")
async def get_shuttle_status(shuttle_id: str = Path(..., description="Идентификатор шаттла")):
    """
    Возвращает текущий статус и информацию о шаттле.
    """
    shuttle = get_shuttle_or_404(shuttle_id)
    return {
        "shuttle_id": shuttle.shuttle_id,
        "name": shuttle.name,
        "ip": shuttle.ip,
        "command_port": shuttle.command_port,
        "is_command_connected": shuttle._command_writer is not None and not shuttle._command_writer.is_closing(),
        "is_status_connected": shuttle.is_status_connected,
        "is_operational": shuttle.is_operational,
        "last_received_status": shuttle.last_status or "Статус не получен",
        "last_f_code": shuttle.last_f_code or "Ошибок F_CODE не зафиксировано",
        "command_queue_size": shuttle._command_queue.qsize()
    }


@app.get("/shuttles", summary="Получить список всех шаттлов и их статусы")
async def list_all_shuttles():
    """
    Возвращает информацию о всех сконфигурированных шаттлах.
    """
    if not shuttles:
        return {"message": "Шаттлы не сконфигурированы."}

    return {
        shuttle_id_key: {
            "name": shuttle_obj.name,
            "ip": shuttle_obj.ip,
            "is_command_connected": shuttle_obj._command_writer is not None and not shuttle_obj._command_writer.is_closing(),
            "is_status_connected": shuttle_obj.is_status_connected,
            "is_operational": shuttle_obj.is_operational,
            "last_received_status": shuttle_obj.last_status or "N/A",
            "last_f_code": shuttle_obj.last_f_code or "N/A",
            "command_queue_size": shuttle_obj._command_queue.qsize()
        } for shuttle_id_key, shuttle_obj in shuttles.items()
    }


if __name__ == "__main__":
    import uvicorn

    # Для запуска: uvicorn имя_файла:app --reload
    uvicorn.run(app, host="0.0.0.0", port=8000, log_level="info")