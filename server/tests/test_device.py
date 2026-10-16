import asyncio

import pytest

from hidagent.device import Device, DeviceError


def test_timeout_grows_with_move_distance_and_text_length():
    assert Device._timeout("click") == 5
    assert Device._timeout("move 1000 -20") > Device._timeout("move 10 10") >= 5
    assert Device._timeout("type " + "x" * 40) > Device._timeout("type hi")


def test_send_without_board_fails_fast():
    with pytest.raises(DeviceError, match="not connected"):
        asyncio.run(Device().send("ping"))


def test_round_trip_with_a_fake_board():
    async def scenario():
        device = Device()
        server = await device.serve("127.0.0.1", 0)
        port = server.sockets[0].getsockname()[1]
        reader, writer = await asyncio.open_connection("127.0.0.1", port)

        async def board():  # answers every command like the firmware does
            while line := await reader.readline():
                writer.write(b"ok\n" if line.strip() != b"bad" else b"err unknown command\n")
                await writer.drain()

        task = asyncio.create_task(board())
        assert await device.wait_connected(2)
        await device.send("ping")
        with pytest.raises(DeviceError, match="unknown command"):
            await device.send("bad")
        assert device.links == 1
        task.cancel()
        writer.close()
        server.close()

    asyncio.run(scenario())


def test_a_new_board_gets_the_pointer_pace_first():
    from hidagent.device import STEP, STEP_MS

    async def scenario():
        device = Device()
        server = await device.serve("127.0.0.1", 0)
        port = server.sockets[0].getsockname()[1]
        reader, writer = await asyncio.open_connection("127.0.0.1", port)
        first = await asyncio.wait_for(reader.readline(), 3)  # the board says nothing, so the server skips "hello"
        assert first == f"tune {STEP} {STEP_MS}\n".encode()
        writer.write(b"ok\n")
        await writer.drain()
        assert await device.wait_connected(2)
        writer.close()
        server.close()

    asyncio.run(scenario())


def test_home_runs_at_full_speed_and_then_restores_the_move_pace():
    from hidagent.device import HOME_STEP_MS, STEP, STEP_MS

    async def scenario():
        device = Device()
        server = await device.serve("127.0.0.1", 0)
        port = server.sockets[0].getsockname()[1]
        reader, writer = await asyncio.open_connection("127.0.0.1", port)
        got = []

        async def board():
            while line := await reader.readline():
                got.append(line.decode().strip())
                writer.write(b"ok\n")
                await writer.drain()

        task = asyncio.create_task(board())
        assert await device.wait_connected(2)
        await asyncio.sleep(0.1)
        got.clear()
        await device.send("home")
        assert got == [f"tune {STEP} {HOME_STEP_MS}", "home", f"tune {STEP} {STEP_MS}"]
        task.cancel()
        writer.close()
        server.close()

    asyncio.run(scenario())
