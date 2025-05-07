# test_gateway.py
import pytest
import httpx
import asyncio
import time

# Configuration matching the gateway and mock shuttle defaults
GATEWAY_API_URL = "http://127.0.0.1:8000" # Where FastAPI runs
MOCK_SHUTTLE_ID = "shuttle1" # Assumes SHUTTLE_CONFIG in main.py has this
MOCK_SHUTTLE_IP = "127.0.0.1" # Needs to match SHUTTLE_CONFIG[MOCK_SHUTTLE_ID]['ip']
MOCK_SHUTTLE_CMD_PORT = 2000 # Needs to match SHUTTLE_CONFIG[MOCK_SHUTTLE_ID]['command_port'] and mock shuttle --listen-port
GATEWAY_STATUS_PORT = 8181 # Needs to match WMS_LISTEN_PORT and mock shuttle --gateway-port

# --- Test Setup ---
# For these tests to work:
# 1. Modify SHUTTLE_CONFIG in main.py so 'shuttle1' uses IP '127.0.0.1' and port 2000.
#    Example:
#    "shuttle1": {
#        "ip": "127.0.0.1", # Changed for local testing
#        "command_port": 2000, # Changed for local testing
#        "status_source_port": 5000,
#        "name": "Mock Shuttle 1"
#    },
# 2. Run the gateway: uvicorn main:app --host 0.0.0.0 --port 8000
# 3. Run the mock shuttle: python mock_shuttle.py --listen-ip 127.0.0.1 --listen-port 2000 --gateway-ip 127.0.0.1 --gateway-port 8181
# 4. Run pytest: pytest test_gateway.py -v -s
#    (-s shows print statements and logs during tests)

# --- Helper ---
async def send_cmd(client: httpx.AsyncClient, shuttle_id: str, command: str) -> httpx.Response:
    url = f"{GATEWAY_API_URL}/shuttle/{shuttle_id}/command"
    print(f"\nTEST: Sending command '{command}' to {shuttle_id} via {url}")
    response = await client.post(url, json={"command": command}, timeout=10.0)
    print(f"TEST: Gateway response status: {response.status_code}")
    try:
        print(f"TEST: Gateway response JSON: {response.json()}")
    except:
         print(f"TEST: Gateway response text: {response.text}")
    return response

# --- Tests ---

@pytest.mark.asyncio
async def test_list_shuttles():
    """Tests the /shuttles endpoint."""
    async with httpx.AsyncClient() as client:
        response = await client.get(f"{GATEWAY_API_URL}/shuttles")
        assert response.status_code == 200
        data = response.json()
        print("TEST /shuttles response:", data)
        assert MOCK_SHUTTLE_ID in data
        assert data[MOCK_SHUTTLE_ID]["ip"] == MOCK_SHUTTLE_IP
        # status_connection_active might be true or false depending on timing
        assert "status_connection_active" in data[MOCK_SHUTTLE_ID]


@pytest.mark.asyncio
async def test_get_shuttle_status_endpoint():
    """Tests the GET /shuttle/{id}/status endpoint."""
    async with httpx.AsyncClient() as client:
        response = await client.get(f"{GATEWAY_API_URL}/shuttle/{MOCK_SHUTTLE_ID}/status")
        assert response.status_code == 200
        data = response.json()
        print(f"TEST /shuttle/{MOCK_SHUTTLE_ID}/status response:", data)
        assert data["shuttle_id"] == MOCK_SHUTTLE_ID
        assert data["ip"] == MOCK_SHUTTLE_IP
        # Check if mock shuttle connected and sent something
        # This requires the mock shuttle to have run and connected back
        # assert data["last_received_status"] != "No status received yet" # This might fail initially


@pytest.mark.asyncio
async def test_send_status_command():
    """Tests sending a STATUS command via the generic endpoint."""
    async with httpx.AsyncClient() as client:
        response = await send_cmd(client, MOCK_SHUTTLE_ID, "STATUS")
        assert response.status_code == 202 # Accepted
        assert "Command 'STATUS' sent" in response.json()["message"]

        # Allow time for mock shuttle to process and send status back
        await asyncio.sleep(1)

        # Check gateway status endpoint again - it *should* now have received status
        response_status = await client.get(f"{GATEWAY_API_URL}/shuttle/{MOCK_SHUTTLE_ID}/status")
        assert response_status.status_code == 200
        data = response_status.json()
        print(f"TEST /shuttle/{MOCK_SHUTTLE_ID}/status after STATUS cmd:", data)
        assert "STATUS=" in data["last_received_status"] # Check if gateway received the status


@pytest.mark.asyncio
async def test_send_pallet_in_command():
    """Tests sending PALLET_IN and checks for status updates."""
    async with httpx.AsyncClient() as client:
        response = await send_cmd(client, MOCK_SHUTTLE_ID, "PALLET_IN")
        assert response.status_code == 202

        # Wait long enough for the mock shuttle to send both STARTED and DONE
        await asyncio.sleep(4) # Should be > max random sleep in mock shuttle

        # Check gateway status - should reflect the *last* status (DONE)
        response_status = await client.get(f"{GATEWAY_API_URL}/shuttle/{MOCK_SHUTTLE_ID}/status")
        assert response_status.status_code == 200
        data = response_status.json()
        print(f"TEST /shuttle/{MOCK_SHUTTLE_ID}/status after PALLET_IN cmd:", data)
        assert data["last_received_status"] == "PALLET_IN_DONE"


@pytest.mark.asyncio
async def test_send_fifo_command():
    """Tests sending a FIFO-NNN command."""
    command = "FIFO-003"
    async with httpx.AsyncClient() as client:
        response = await send_cmd(client, MOCK_SHUTTLE_ID, command)
        assert response.status_code == 202

        # Wait for mock processing
        await asyncio.sleep(3) # Adjust based on mock shuttle sleep time

        response_status = await client.get(f"{GATEWAY_API_URL}/shuttle/{MOCK_SHUTTLE_ID}/status")
        assert response_status.status_code == 200
        data = response_status.json()
        print(f"TEST /shuttle/{MOCK_SHUTTLE_ID}/status after {command} cmd:", data)
        assert data["last_received_status"] == f"{command}_DONE"


@pytest.mark.asyncio
async def test_send_command_unknown_shuttle():
    """Tests sending a command to a non-existent shuttle."""
    async with httpx.AsyncClient() as client:
        url = f"{GATEWAY_API_URL}/shuttle/unknown_shuttle/command"
        response = await client.post(url, json={"command": "STATUS"})
        assert response.status_code == 404


# Add more tests for other commands (BATTERY, HOME, COUNT, etc.)
# Add tests for error conditions (e.g., stopping the mock shuttle and trying to send)