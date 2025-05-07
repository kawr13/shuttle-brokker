import asyncio
import logging
import argparse
import random
import time
from collections import deque

# Configuration
MESSAGE_TERMINATOR = b'\n'
ENCODING = 'utf-8'

logging.basicConfig(
    level=logging.INFO,
    format='%(asctime)s - %(levelname)s - MOCK_SHUTTLE - %(message)s'
)
logger = logging.getLogger("MockShuttle")

# --- Shuttle State Simulation ---
shuttle_state = {
    "last_command": None,
    "status": "FREE",  # FREE, CARGO, BUSY, NOT_READY
    "battery": 100,
    "location": "A1-HOME",
    "pallets": 5,
    "wdh": 54,
    "wlh": 28,
    "error_code": None,
    "status_connection_active": False,
    "gateway_writer": None,
    "unacked_messages": deque()  # Queue for unacknowledged messages
}
status_connection_lock = asyncio.Lock()


async def send_to_gateway(message: str):
    """Sends a message to the connected gateway status port with reconnection attempt."""
    async with status_connection_lock:
        writer = shuttle_state.get("gateway_writer")
        if not writer or writer.is_closing() or not shuttle_state["status_connection_active"]:
            logger.warning("No active connection to gateway, attempting to reconnect...")
            gateway_ip, gateway_port = shuttle_state["gateway_address"]
            await connect_to_gateway_status_port(gateway_ip, gateway_port)
            writer = shuttle_state.get("gateway_writer")

        if writer and not writer.is_closing():
            try:
                full_message = message.encode(ENCODING) + MESSAGE_TERMINATOR
                logger.info(f"Sending to Gateway: {message}")
                writer.write(full_message)
                await writer.drain()
                shuttle_state["unacked_messages"].append(message)  # Add to unacked queue
                return True
            except Exception as e:
                logger.error(f"Error sending to gateway: {e}")
                shuttle_state["status_connection_active"] = False
                shuttle_state["gateway_writer"] = None
                return False
        else:
            logger.warning(f"Cannot send '{message}', no active gateway connection.")
            return False


async def handle_gateway_mrcd(reader: asyncio.StreamReader):
    """Listens for MRCD from the gateway and clears unacked messages."""
    try:
        while shuttle_state["status_connection_active"]:
            if reader.at_eof():
                logger.warning("Gateway closed MRCD connection.")
                break
            try:
                line_bytes = await asyncio.wait_for(reader.readline(), timeout=10.0)
                if not line_bytes:
                    break
                message = line_bytes.strip().decode(ENCODING)
                if message == "MRCD" and shuttle_state["unacked_messages"]:
                    logger.info("Received MRCD, clearing oldest unacked message.")
                    shuttle_state["unacked_messages"].popleft()
                else:
                    logger.warning(f"Unexpected message: {message}")
            except asyncio.TimeoutError:
                logger.warning("Timeout waiting for MRCD.")
            except Exception as e:
                logger.error(f"Error reading MRCD: {e}")
                break
    finally:
        logger.info("MRCD listener stopped.")


async def connect_to_gateway_status_port(gateway_ip: str, gateway_port: int):
    """Establishes connection to gateway status port."""
    async with status_connection_lock:
        if shuttle_state.get("gateway_writer") and not shuttle_state["gateway_writer"].is_closing():
            return True
        try:
            reader, writer = await asyncio.wait_for(
                asyncio.open_connection(gateway_ip, gateway_port),
                timeout=5.0
            )
            shuttle_state["gateway_writer"] = writer
            shuttle_state["status_connection_active"] = True
            logger.info(f"Connected to Gateway at {writer.get_extra_info('peername')}")
            asyncio.create_task(handle_gateway_mrcd(reader))
            return True
        except Exception as e:
            logger.error(f"Failed to connect to {gateway_ip}:{gateway_port}: {e}")
            shuttle_state["status_connection_active"] = False
            shuttle_state["gateway_writer"] = None
            return False


async def check_connection_periodically():
    """Periodically checks and re-establishes gateway connection."""
    while True:
        if not shuttle_state["status_connection_active"]:
            gateway_ip, gateway_port = shuttle_state["gateway_address"]
            await connect_to_gateway_status_port(gateway_ip, gateway_port)
        await asyncio.sleep(10)  # Check every 10 seconds


async def process_command(command: str, gateway_ip: str, gateway_port: int):
    """Simulates shuttle actions based on command."""
    logger.info(f"Processing command: {command}")
    shuttle_state["last_command"] = command
    shuttle_state["error_code"] = None

    if not shuttle_state["status_connection_active"]:
        await connect_to_gateway_status_port(gateway_ip, gateway_port)
        if not shuttle_state["status_connection_active"]:
            logger.error("Failed to connect to gateway.")
            return

    # Simulate random error (5% chance)
    if random.random() < 0.05:
        await send_to_gateway(f"{command}_ABORT")
        shuttle_state["error_code"] = "F_CODE=2"
        return

    if command == "PALLET_IN":
        shuttle_state["status"] = "BUSY"
        await send_to_gateway("PALLET_IN_STARTED")
        await asyncio.sleep(random.uniform(1, 3))
        shuttle_state["status"] = "CARGO"  # Reflects loaded pallet
        shuttle_state["pallets"] += 1
        await send_to_gateway("PALLET_IN_DONE")
    elif command == "PALLET_OUT":
        if shuttle_state["pallets"] > 0:
            shuttle_state["status"] = "BUSY"
            await send_to_gateway("PALLET_OUT_STARTED")
            await asyncio.sleep(random.uniform(1, 3))
            shuttle_state["status"] = "FREE"
            shuttle_state["pallets"] -= 1
            await send_to_gateway("PALLET_OUT_DONE")
        else:
            await send_to_gateway("PALLET_OUT_ABORT")
    elif command.startswith("FIFO-"):
        try:
            num = int(command.split('-')[1])
            if num <= 0: raise ValueError
            shuttle_state["status"] = "BUSY"
            await send_to_gateway(f"{command}_STARTED")
            await asyncio.sleep(random.uniform(1, num * 0.5))
            shuttle_state["status"] = "FREE"
            await send_to_gateway(f"{command}_DONE")
        except (IndexError, ValueError):
            logger.error(f"Invalid FIFO command: {command}")
            await send_to_gateway(f"{command}_ABORT")
    elif command == "HOME":
        shuttle_state["status"] = "BUSY"
        await send_to_gateway("HOME_STARTED")
        await asyncio.sleep(random.uniform(0.5, 2))
        shuttle_state["status"] = "FREE"
        shuttle_state["location"] = f"LOC-{random.randint(1000, 9999)}"
        await send_to_gateway(f"LOCATION={shuttle_state['location']}")
    elif command == "STATUS":
        await send_to_gateway(f"STATUS={shuttle_state['status']}")
    elif command == "BATTERY":
        shuttle_state["battery"] = max(0, shuttle_state["battery"] - random.uniform(0, 2))
        bat_level = shuttle_state["battery"]
        status_str = f"BATTERY={int(bat_level)}%"
        if bat_level <= 5 and not shuttle_state["error_code"]:
            shuttle_state["error_code"] = "F_CODE=1"
            await send_to_gateway(shuttle_state["error_code"])
        await send_to_gateway(status_str)
    else:
        logger.warning(f"Unknown command: {command}")
        await send_to_gateway(f"{command}_ABORT")


async def handle_command_connection(reader: asyncio.StreamReader, writer: asyncio.StreamWriter):
    """Handles incoming gateway connection on command port."""
    peername = writer.get_extra_info('peername')
    logger.info(f"Gateway connected from {peername}")
    gateway_ip, gateway_port = shuttle_state["gateway_address"]

    try:
        while True:
            if reader.at_eof():
                break
            line_bytes = await asyncio.wait_for(reader.readline(), timeout=300.0)
            if not line_bytes:
                break
            command = line_bytes.strip().decode(ENCODING)
            asyncio.create_task(process_command(command, gateway_ip, gateway_port))
    except Exception as e:
        logger.error(f"Command connection error: {e}")
    finally:
        writer.close()
        await writer.wait_closed()
        logger.info(f"Closed connection from {peername}")


async def main(listen_ip: str, listen_port: int, gateway_ip: str, gateway_status_port: int):
    """Starts the mock shuttle server."""
    shuttle_state["gateway_address"] = (gateway_ip, gateway_status_port)
    await connect_to_gateway_status_port(gateway_ip, gateway_status_port)
    asyncio.create_task(check_connection_periodically())

    server = await asyncio.start_server(
        handle_command_connection,
        listen_ip,
        listen_port
    )

    addr = server.sockets[0].getsockname()
    logger.info(f"Mock Shuttle listening on {addr}")
    async with server:
        await server.serve_forever()


if __name__ == "__main__":
    parser = argparse.ArgumentParser(description="Mock Shuttle Simulator")
    parser.add_argument("--listen-ip", default="127.0.0.1", help="IP to listen on")
    parser.add_argument("--listen-port", type=int, default=2000, help="Port for commands")
    parser.add_argument("--gateway-ip", default="127.0.0.1", help="Gateway IP")
    parser.add_argument("--gateway-port", type=int, default=8181, help="Gateway status port")
    args = parser.parse_args()

    asyncio.run(main(args.listen_ip, args.listen_port, args.gateway_ip, args.gateway_port))