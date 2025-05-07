import asyncio
import logging
from typing import Dict, Optional, Tuple, Any, AsyncGenerator
from contextlib import asynccontextmanager
from enum import Enum
from fastapi import FastAPI, HTTPException, Path, Body
from pydantic import BaseModel
import heapq  # For Priority Queue items

# --- Конфигурация ---
WMS_LISTEN_IP = "0.0.0.0"  # IP-адрес, на котором WMS слушает статусы от шаттлов
WMS_LISTEN_PORT = 8181  # Порт, на котором WMS слушает статусы
SHUTTLE_CONFIG = {
    "shuttle1": {"ip": "127.0.0.1", "command_port": 2000, "name": "Mock Shuttle 1"},
    # Добавьте реальные IP и порты для других шаттлов, если они отличаются от status_source_port
    # "shuttle2": {"ip": "10.10.10.112", "command_port": 2000, "name": "Shuttle 2"},
    # "shuttle3": {"ip": "10.10.10.113", "command_port": 2000, "name": "Shuttle 3"},
}
MESSAGE_TERMINATOR = b'\n'  # Терминатор сообщений
ENCODING = 'utf-8'  # Кодировка сообщений

COMMAND_ACK_TIMEOUT = 15.0  # Таймаут ожидания подтверждения команды (в секундах)
RECONNECT_DELAY = 5  # Задержка перед попыткой переподключения (в секундах)

# Настройка логирования
logging.basicConfig(
    level=logging.INFO,
    format='%(asctime)s - %(levelname)s - [%(name)s] - %(message)s',
    datefmt='%Y-%m-%d %H:%M:%S'
)
logger = logging.getLogger("WMS_Gateway")

# --- Коды ошибок шаттла (F_CODE) ---
F_CODE_DESCRIPTIONS = {
    1: "Аккумулятор разряжен",
    2: "Превышение времени подъема/опускания платформы",
    3: "Отсутствие движения ВПЕРЁД: нет вращения паразитного колеса (упёрся) при движении вперёд",
    4: "Отсутствие движения: нет вращения паразитного колеса (опция), или застрял при движении",
    5: "Ошибка серводвигателя перемещения",
    6: "Ошибка серводвигателя подъема/опускания платформы",
    8: "Шаттл не в канале, режим НОМЕ невозможен",
    9: "Шаттл не готов к выполнению автоматических функций",
}


# --- Приоритеты команд ---
class CommandPriority(Enum):
    HIGH = 0  # Для критических команд (HOME)
    MEDIUM = 1  # Для запросов состояния (STATUS, BATTERY)
    NORMAL = 2  # Для стандартных операций (PALLET_IN, FIFO)


# --- Команды шаттла ---
class ShuttleCommand(str, Enum):
    PALLET_IN = "PALLET_IN"
    PALLET_OUT = "PALLET_OUT"
    FIFO = "FIFO-{count}"
    FILO = "FILO-{count}"
    STACK_IN = "STACK_IN"
    STACK_OUT = "STACK_OUT"
    HOME = "HOME"
    COUNT = "COUNT"
    STATUS = "STATUS"
    BATTERY = "BATTERY"
    WDH = "WDH"  # Запрос часов работы привода перемещения
    WLH = "WLH"  # Запрос часов работы привода подъема

    @classmethod
    def get_priority(cls, command_value: str) -> int:
        command_base = command_value.split('-')[0]  # e.g. FIFO from FIFO-NNN
        if command_base == cls.HOME.value:
            return CommandPriority.HIGH.value
        elif command_base in [cls.STATUS.value, cls.BATTERY.value, cls.WDH.value, cls.WLH.value]:
            return CommandPriority.MEDIUM.value
        return CommandPriority.NORMAL.value


# --- Исключения ---
class ShuttleConnectionError(Exception):
    pass


class ShuttleCommandError(Exception):
    pass


# --- Класс для управления одним шаттлом ---
class Shuttle:
    def __init__(self, shuttle_id: str, ip: str, command_port: int, name: str):
        self.shuttle_id = shuttle_id
        self.ip = ip
        self.command_port = command_port
        self.name = name
        self.logger = logging.getLogger(f"Shuttle-{self.name}")

        self._command_reader: Optional[asyncio.StreamReader] = None
        self._command_writer: Optional[asyncio.StreamWriter] = None
        self._command_conn_lock = asyncio.Lock()  # Блокировка для операций с командным соединением

        self._status_reader: Optional[asyncio.StreamReader] = None
        self._status_writer: Optional[asyncio.StreamWriter] = None
        self._status_conn_lock = asyncio.Lock()  # Блокировка для операций со статусным соединением
        self._status_handler_task: Optional[asyncio.Task] = None

        self.command_queue = asyncio.PriorityQueue()  # Очередь команд с приоритетами
        self._processing_task: Optional[asyncio.Task] = None

        self._pending_command: Optional[str] = None  # Команда, ожидающая подтверждения
        self._command_ack_event = asyncio.Event()  # Событие для сигнализации получения ответа на команду

        self.last_status: Optional[str] = None
        self.last_status_timestamp: float = 0.0
        self.current_f_code: Optional[str] = None
        self.is_command_connected: bool = False
        self.is_status_connected: bool = False

        self.logger.info(f"Инициализирован. IP для команд: {self.ip}:{self.command_port}")

    async def _ensure_command_connection(self):
        if self._command_writer and not self._command_writer.is_closing():
            return  # Соединение уже установлено

        async with self._command_conn_lock:
            # Повторная проверка внутри блокировки
            if self._command_writer and not self._command_writer.is_closing():
                return

            self.logger.info("Попытка установки командного соединения...")
            try:
                self._command_reader, self._command_writer = await asyncio.wait_for(
                    asyncio.open_connection(self.ip, self.command_port), timeout=5.0
                )
                self.is_command_connected = True
                self.logger.info("Командное соединение установлено.")
            except asyncio.TimeoutError:
                self.logger.error("Таймаут при установке командного соединения.")
                await self._close_command_connection_internal()
                raise ShuttleConnectionError(f"Таймаут подключения к {self.name}")
            except ConnectionRefusedError:
                self.logger.error("Отказано в командном соединении.")
                await self._close_command_connection_internal()
                raise ShuttleConnectionError(f"Отказано в подключении к {self.name}")
            except Exception as e:
                self.logger.error(f"Ошибка командного подключения: {e}", exc_info=True)
                await self._close_command_connection_internal()
                raise ShuttleConnectionError(f"Ошибка подключения к {self.name}: {e}")

    async def _send_command_internal(self, command: str, retries: int = 2) -> bool:
        for attempt in range(retries + 1):
            try:
                await self._ensure_command_connection()
                if not self._command_writer or self._command_writer.is_closing():
                    self.logger.warning("Командное соединение неактивно для отправки.")
                    raise ShuttleConnectionError("Командный писатель не доступен")

                full_message = command.encode(ENCODING) + MESSAGE_TERMINATOR
                self._command_writer.write(full_message)
                await self._command_writer.drain()
                self.logger.info(f"Команда '{command}' успешно отправлена (попытка {attempt + 1}).")
                return True
            except Exception as e:
                self.logger.error(f"Попытка {attempt + 1}/{retries + 1} отправки команды '{command}' не удалась: {e}")
                await self._close_command_connection_internal()  # Закрываем при ошибке для переподключения
                if attempt < retries:
                    await asyncio.sleep(1 + attempt)  # Небольшая задержка перед повторной попыткой
                else:
                    self.logger.error(f"Все попытки отправки команды '{command}' исчерпаны.")
                    return False
        return False  # Should not be reached if retries >=0

    async def send_command_to_shuttle(self, command: str, params: Optional[Dict[str, Any]] = None):
        """Формирует и добавляет команду в очередь."""
        if params is None:
            params = {}

        cmd_enum_member = ShuttleCommand[command.upper().split('-')[0]]  # e.g. FIFO from FIFO-NNN
        command_str_template = cmd_enum_member.value

        if "{count}" in command_str_template:
            count = params.get("count")
            if not isinstance(count, int) or count < 1:
                raise ValueError("Для команд FIFO/FILO требуется параметр 'count' (целое число > 0)")
            final_command_str = command_str_template.format(count=count)
        else:
            final_command_str = command_str_template

        priority = ShuttleCommand.get_priority(final_command_str)

        # asyncio.PriorityQueue ожидает элементы в виде (priority, data)
        await self.command_queue.put((priority, final_command_str))
        self.logger.info(f"Команда '{final_command_str}' (Приоритет: {priority}) добавлена в очередь.")

        # Запускаем задачу обработки очереди, если она еще не запущена или завершилась
        if not self._processing_task or self._processing_task.done():
            if self._processing_task and self._processing_task.done() and self._processing_task.exception():
                try:
                    self._processing_task.result()  # Перевызовет исключение, если оно было
                except Exception as e:
                    self.logger.error(f"Задача обработки команд ранее завершилась с ошибкой: {e}. Перезапускаем.",
                                      exc_info=True)
            self._processing_task = asyncio.create_task(self._process_command_queue())
            self.logger.info("Задача обработки команд запущена/перезапущена.")

    async def _process_command_queue(self):
        """Обрабатывает команды из очереди."""
        try:
            while True:
                _priority, command_to_process = await self.command_queue.get()
                self.logger.info(f"Извлечена команда '{command_to_process}' из очереди.")
                self._pending_command = command_to_process
                self._command_ack_event.clear()  # Сбрасываем событие перед отправкой новой команды

                sent_successfully = await self._send_command_internal(command_to_process)

                if sent_successfully:
                    self.logger.info(
                        f"Ожидание подтверждения/статуса для команды '{command_to_process}' (таймаут: {COMMAND_ACK_TIMEOUT}с).")
                    try:
                        await asyncio.wait_for(self._command_ack_event.wait(), timeout=COMMAND_ACK_TIMEOUT)
                        self.logger.info(
                            f"Подтверждение/статус получен для '{command_to_process}'. Последний статус: {self.last_status}")
                    except asyncio.TimeoutError:
                        self.logger.warning(
                            f"Таймаут ожидания подтверждения/статуса для команды '{command_to_process}'.")
                        if self._pending_command == command_to_process:  # Если команда все еще ожидается
                            self._pending_command = None  # Считаем, что команда "потеряна" для этого цикла ACK
                else:
                    self.logger.error(f"Не удалось отправить команду '{command_to_process}'.")
                    if self._pending_command == command_to_process:
                        self._pending_command = None  # Очищаем, так как отправка не удалась

                self.command_queue.task_done()  # Сообщаем очереди, что элемент обработан
        except asyncio.CancelledError:
            self.logger.info("Задача обработки команд отменена.")
            raise  # Важно перевызвать CancelledError
        except Exception as e:
            self.logger.error(f"Критическая ошибка в задаче обработки команд: {e}", exc_info=True)
            # Задача завершится, send_command_to_shuttle ее перезапустит при следующей команде

    def _parse_and_log_f_code(self, f_code_message: str):
        try:
            code_num_str = f_code_message.split('=')[1]
            code_num = int(code_num_str)
            description = F_CODE_DESCRIPTIONS.get(code_num, "Неизвестный код ошибки")
            self.logger.error(f"Ошибка шаттла: {f_code_message} - {description}")
            self.current_f_code = f_code_message  # Сохраняем полный код ошибки
        except (IndexError, ValueError) as e:
            self.logger.error(f"Ошибка парсинга F_CODE '{f_code_message}': {e}")
            self.current_f_code = f_code_message  # Сохраняем как есть

    async def _handle_incoming_status(self):
        """Обрабатывает входящие сообщения о статусе от шаттла."""
        if not self._status_reader or not self._status_writer:
            self.logger.error("Отсутствует reader/writer для обработки статуса.")
            self.is_status_connected = False
            return

        peername = self._status_writer.get_extra_info('peername', ('?', '?'))
        self.logger.info(f"Запуск обработчика статуса для соединения от {peername[0]}:{peername[1]}")
        self.is_status_connected = True
        self.current_f_code = None  # Сбрасываем F_CODE при новом соединении/обработке

        try:
            while True:
                try:
                    data = await asyncio.wait_for(self._status_reader.readline(),
                                                  timeout=60.0)  # Таймаут на чтение строки
                except asyncio.TimeoutError:
                    self.logger.warning("Таймаут чтения статусного сообщения. Проверка соединения.")
                    # Попытка отправить "пустое" сообщение для проверки жизнеспособности соединения
                    try:
                        self._status_writer.write(MESSAGE_TERMINATOR)
                        await self._status_writer.drain()
                        continue  # Если успешно, продолжаем ожидать данные
                    except Exception:
                        self.logger.error("Статусное соединение потеряно при проверке.")
                        break  # Выход из цикла, соединение будет закрыто

                if not data:
                    self.logger.info("Статусное соединение закрыто удаленной стороной.")
                    break  # Соединение закрыто

                message = data.decode(ENCODING).strip()
                if not message: continue  # Пропускаем пустые строки

                self.logger.info(f"Получен статус: {message}")
                self.last_status = message
                self.last_status_timestamp = asyncio.get_event_loop().time()

                event_triggered = False
                command_cleared_by_status = False

                if message.startswith("F_CODE="):
                    self._parse_and_log_f_code(message)
                    if self._pending_command:
                        self.logger.warning(
                            f"F_CODE '{message}' получен во время ожидания ответа на '{self._pending_command}'.")
                    event_triggered = True
                    command_cleared_by_status = True  # F_CODE прерывает текущую команду

                elif self._pending_command:
                    cmd_root = self._pending_command.split('-')[0]
                    # Проверяем, относится ли сообщение к ожидаемой команде
                    if message.startswith(cmd_root) or \
                            (cmd_root == "COUNT" and message.startswith("COUNT_")) or \
                            (cmd_root == "HOME" and message.startswith("LOCATION=")):

                        if message.endswith("_STARTED"):
                            self.logger.info(f"Команда '{self._pending_command}' запущена: {message}")
                            event_triggered = True
                            # Не очищаем _pending_command на _STARTED
                        elif message.endswith(("_DONE", "_ABORT")):
                            self.logger.info(f"Команда '{self._pending_command}' завершена/прервана: {message}")
                            event_triggered = True
                            command_cleared_by_status = True
                        # Ответы на команды-запросы
                        elif cmd_root == "STATUS" and message.startswith("STATUS="):
                            event_triggered = True;
                            command_cleared_by_status = True
                        elif cmd_root == "BATTERY" and message.startswith("BATTERY="):
                            event_triggered = True;
                            command_cleared_by_status = True
                        elif cmd_root == "WDH" and message.startswith("WDH="):
                            event_triggered = True;
                            command_cleared_by_status = True
                        elif cmd_root == "WLH" and message.startswith("WLH="):
                            event_triggered = True;
                            command_cleared_by_status = True
                        elif cmd_root == "HOME" and message.startswith("LOCATION="):
                            event_triggered = True  # LOCATION - часть HOME, не финальный статус
                        elif cmd_root == "COUNT" and message.startswith("COUNT_") and "=N" in message:  # COUNT_ID=NNN
                            event_triggered = True;
                            command_cleared_by_status = True

                        if event_triggered:
                            self.logger.info(
                                f"Статус '{message}' связан с ожидаемой командой '{self._pending_command}'.")

                if event_triggered:
                    self._command_ack_event.set()
                    if command_cleared_by_status and self._pending_command:
                        self.logger.info(
                            f"Ожидаемая команда '{self._pending_command}' очищена из-за статуса '{message}'.")
                        self._pending_command = None

                await self._send_mrcd()  # Подтверждаем получение любого сообщения

        except ConnectionResetError:
            self.logger.warning("Статусное соединение сброшено.")
        except asyncio.CancelledError:
            self.logger.info("Обработчик статусов отменен.")
            raise
        except Exception as e:
            self.logger.error(f"Ошибка в обработчике статусов: {e}", exc_info=True)
        finally:
            self.logger.info("Завершение работы обработчика статусов.")
            self.is_status_connected = False
            # Не закрываем здесь соединение, пусть это делает внешний код (set_status_connection или shutdown)

    async def _send_mrcd(self):
        """Отправляет MRCD (Message Received) подтверждение шаттлу."""
        async with self._status_conn_lock:  # Используем блокировку для доступа к _status_writer
            if self._status_writer and not self._status_writer.is_closing():
                try:
                    mrcd_message = "MRCD".encode(ENCODING) + MESSAGE_TERMINATOR
                    self._status_writer.write(mrcd_message)
                    await self._status_writer.drain()
                    self.logger.debug("Отправлено подтверждение MRCD.")
                except Exception as e:
                    self.logger.error(f"Ошибка отправки MRCD: {e}. Закрываем статусное соединение.")
                    # При ошибке отправки MRCD, вероятно, соединение уже недействительно
                    await self._close_status_connection_internal()  # Закрываем изнутри, если критично
            else:
                self.logger.warning("Попытка отправить MRCD при неактивном статусном соединении.")

    async def set_status_connection(self, reader: asyncio.StreamReader, writer: asyncio.StreamWriter):
        """Устанавливает новое соединение для получения статусов."""
        async with self._status_conn_lock:  # Блокировка на время смены соединения
            # Закрываем предыдущее соединение и задачу, если они есть
            if self._status_handler_task and not self._status_handler_task.done():
                self._status_handler_task.cancel()
                try:
                    await self._status_handler_task
                except asyncio.CancelledError:
                    self.logger.info("Предыдущий обработчик статусов успешно отменен.")
                except Exception as e:
                    self.logger.error(f"Ошибка при отмене предыдущего обработчика статусов: {e}", exc_info=True)

            await self._close_status_connection_internal()  # Убедимся, что старый writer закрыт

            self._status_reader = reader
            self._status_writer = writer
            self.is_status_connected = True
            self.logger.info("Новое статусное соединение установлено.")
            self._status_handler_task = asyncio.create_task(self._handle_incoming_status())

    async def _close_command_connection_internal(self):
        """Внутренний метод закрытия командного соединения (без внешней блокировки)."""
        if self._command_writer:
            self.logger.info("Закрытие командного соединения...")
            try:
                if not self._command_writer.is_closing():
                    self._command_writer.close()
                    await self._command_writer.wait_closed()
            except Exception as e:
                self.logger.error(f"Ошибка при закрытии командного соединения: {e}", exc_info=True)
            finally:
                self._command_reader = None
                self._command_writer = None
                self.is_command_connected = False
                self.logger.info("Командное соединение закрыто.")

    async def close_command_connection(self):
        """Публичный метод закрытия командного соединения."""
        async with self._command_conn_lock:
            await self._close_command_connection_internal()

    async def _close_status_connection_internal(self):
        """Внутренний метод закрытия статусного соединения (без внешней блокировки)."""
        if self._status_writer:
            self.logger.info("Закрытие статусного соединения...")
            try:
                if not self._status_writer.is_closing():
                    self._status_writer.close()
                    await self._status_writer.wait_closed()
            except Exception as e:
                self.logger.error(f"Ошибка при закрытии статусного соединения: {e}", exc_info=True)
            finally:
                self._status_reader = None
                self._status_writer = None
                self.is_status_connected = False
                self.logger.info("Статусное соединение закрыто.")

    async def close_status_connection(self):
        """Публичный метод закрытия статусного соединения и его обработчика."""
        if self._status_handler_task and not self._status_handler_task.done():
            self._status_handler_task.cancel()
            try:
                await self._status_handler_task
            except asyncio.CancelledError:
                self.logger.info("Обработчик статусов отменен при закрытии.")
            except Exception as e:
                self.logger.error(f"Ошибка при отмене обработчика статусов при закрытии: {e}", exc_info=True)

        async with self._status_conn_lock:
            await self._close_status_connection_internal()

    async def cleanup(self):
        """Очистка всех ресурсов шаттла."""
        self.logger.info("Начало очистки ресурсов...")
        if self._processing_task and not self._processing_task.done():
            self._processing_task.cancel()
            try:
                await self._processing_task
            except asyncio.CancelledError:
                self.logger.info("Задача обработки команд успешно отменена при очистке.")
            except Exception as e:
                self.logger.error(f"Ошибка при отмене задачи обработки команд: {e}", exc_info=True)

        await self.close_command_connection()
        await self.close_status_connection()  # Это также отменит _status_handler_task
        self.logger.info("Очистка ресурсов завершена.")


# --- Глобальное состояние ---
shuttles_registry: Dict[str, Shuttle] = {}
tcp_status_server: Optional[asyncio.AbstractServer] = None


# --- TCP сервер для приема статусов от шаттлов ---
async def handle_shuttle_status_connection(reader: asyncio.StreamReader, writer: asyncio.StreamWriter):
    peername = writer.get_extra_info('peername')
    if not peername:
        logger.error("Не удалось определить IP адрес подключающегося шаттла. Закрытие соединения.")
        writer.close()
        await writer.wait_closed()
        return

    peer_ip, _ = peername
    logger.info(f"Получено входящее статусное соединение от {peer_ip}")

    shuttle_found = None
    for shuttle_obj in shuttles_registry.values():
        # ВАЖНО: IP шаттла для команд может отличаться от IP, с которого он шлет статус,
        # если шаттл за NAT или имеет несколько интерфейсов.
        # Здесь предполагается, что IP, с которого шаттл инициирует статусное соединение,
        # совпадает с IP, указанным в SHUTTLE_CONFIG для команд.
        # Если это не так, нужна более сложная логика идентификации (например, по первому сообщению от шаттла).
        if shuttle_obj.ip == peer_ip:
            shuttle_found = shuttle_obj
            break

    if shuttle_found:
        logger.info(f"Соединение от {peer_ip} сопоставлено с шаттлом {shuttle_found.name}.")
        await shuttle_found.set_status_connection(reader, writer)
    else:
        logger.warning(f"Неизвестный IP адрес шаттла: {peer_ip}. Закрытие соединения.")
        writer.close()
        await writer.wait_closed()


# --- FastAPI приложение ---
@asynccontextmanager
async def lifespan(_app: FastAPI) -> AsyncGenerator[None, None]:
    global tcp_status_server, shuttles_registry
    logger.info("Запуск шлюза WMS-Шаттл...")

    for shuttle_id, config in SHUTTLE_CONFIG.items():
        shuttles_registry[shuttle_id] = Shuttle(
            shuttle_id=shuttle_id,
            ip=config["ip"],
            command_port=config["command_port"],
            name=config["name"]
        )

    try:
        server = await asyncio.start_server(
            handle_shuttle_status_connection, WMS_LISTEN_IP, WMS_LISTEN_PORT
        )
        addr = server.sockets[0].getsockname() if server.sockets else 'N/A'
        logger.info(f"TCP сервер для статусов слушает на {addr}")
        tcp_status_server = server
    except Exception as e:
        logger.error(f"Ошибка запуска TCP сервера статусов: {e}", exc_info=True)
        # Можно решить, стоит ли продолжать работу FastAPI без TCP сервера
        # raise # Или остановить запуск приложения

    yield  # Приложение работает

    logger.info("Остановка шлюза WMS-Шаттл...")
    if tcp_status_server:
        tcp_status_server.close()
        await tcp_status_server.wait_closed()
        logger.info("TCP сервер статусов остановлен.")

    for shuttle_id, shuttle_obj in shuttles_registry.items():
        logger.info(f"Остановка шаттла {shuttle_obj.name}...")
        await shuttle_obj.cleanup()
    shuttles_registry.clear()
    logger.info("Шлюз WMS-Шаттл полностью остановлен.")


app = FastAPI(title="WMS-Shuttle Gateway", version="1.1.0", lifespan=lifespan)


def get_shuttle_or_404(shuttle_id: str) -> Shuttle:
    shuttle = shuttles_registry.get(shuttle_id)
    if not shuttle:
        raise HTTPException(status_code=404, detail=f"Шаттл '{shuttle_id}' не найден.")
    return shuttle


class CommandPayload(BaseModel):
    command: str  # Имя команды из ShuttleCommand, например "PALLET_IN" или "FIFO"
    params: Dict[str, Any] = {}


@app.post("/shuttle/{shuttle_id}/command", status_code=202, summary="Отправить команду шаттлу")
async def send_shuttle_command_endpoint(
        shuttle_id: str = Path(..., description="Идентификатор шаттла"),
        payload: CommandPayload = Body(...)
):
    """
    Отправляет команду указанному шаттлу.
    Для команд `FIFO` или `FILO` необходимо передать `count` в `params`.
    Пример: `{"command": "FIFO", "params": {"count": 5}}`
    """
    shuttle = get_shuttle_or_404(shuttle_id)

    # Проверка, что команда существует в ShuttleCommand
    command_base_name = payload.command.upper().split('-')[0]
    try:
        ShuttleCommand[command_base_name]
    except KeyError:
        raise HTTPException(status_code=400, detail=f"Недопустимая команда: {payload.command}")

    try:
        await shuttle.send_command_to_shuttle(payload.command, payload.params)
        return {"message": f"Команда '{payload.command}' для шаттла '{shuttle_id}' добавлена в очередь."}
    except ValueError as ve:  # Ошибка валидации параметров команды
        raise HTTPException(status_code=400, detail=str(ve))
    except Exception as e:
        shuttle.logger.error(f"Ошибка при постановке команды '{payload.command}' в очередь: {e}", exc_info=True)
        raise HTTPException(status_code=500, detail=f"Внутренняя ошибка сервера при обработке команды: {e}")


@app.get("/shuttle/{shuttle_id}/status", summary="Получить статус шаттла")
async def get_shuttle_status_endpoint(shuttle_id: str = Path(..., description="Идентификатор шаттла")):
    shuttle = get_shuttle_or_404(shuttle_id)
    return {
        "shuttle_id": shuttle.shuttle_id,
        "name": shuttle.name,
        "ip": shuttle.ip,
        "command_port": shuttle.command_port,
        "is_command_connected": shuttle.is_command_connected,
        "is_status_connected": shuttle.is_status_connected,
        "last_received_status": shuttle.last_status or "Статус еще не получен",
        "last_status_timestamp": shuttle.last_status_timestamp if shuttle.last_status_timestamp else "N/A",
        "pending_command": shuttle._pending_command,
        "current_f_code": shuttle.current_f_code,
        "command_queue_size": shuttle.command_queue.qsize(),
    }


@app.get("/shuttles", summary="Получить список всех шаттлов и их статусы")
async def list_all_shuttles_endpoint():
    return {
        shuttle_id: {
            "name": s.name,
            "is_command_connected": s.is_command_connected,
            "is_status_connected": s.is_status_connected,
            "last_received_status": s.last_status or "N/A",
            "current_f_code": s.current_f_code,
            "pending_command": s._pending_command,
            "command_queue_size": s.command_queue.qsize(),
        }
        for shuttle_id, s in shuttles_registry.items()
    }


@app.get("/health", summary="Проверка работоспособности шлюза")
async def health_check_endpoint():
    shuttle_health_statuses = {}
    for shuttle_id, shuttle in shuttles_registry.items():
        shuttle_health_statuses[shuttle_id] = {
            "command_connection": shuttle.is_command_connected,
            "status_connection": shuttle.is_status_connected,
            "last_status_age_seconds": (
                        asyncio.get_event_loop().time() - shuttle.last_status_timestamp) if shuttle.last_status_timestamp else "N/A",
            "has_f_code": bool(shuttle.current_f_code)
        }
    return {
        "gateway_status": "healthy" if tcp_status_server and tcp_status_server.is_serving() else "degraded",
        "tcp_status_server_listening": tcp_status_server.is_serving() if tcp_status_server else False,
        "shuttles": shuttle_health_statuses
    }


if __name__ == "__main__":
    import uvicorn

    logger.info("Запуск шлюза WMS-Шаттл через Uvicorn...")
    # Для production рекомендуется использовать Gunicorn + Uvicorn workers:
    # gunicorn your_module_name:app -w 4 -k uvicorn.workers.UvicornWorker -b 0.0.0.0:8000
    uvicorn.run(app, host="0.0.0.0", port=8000, log_level="info")