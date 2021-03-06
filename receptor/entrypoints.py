import asyncio
import logging
import sys
import time

from prometheus_client import start_http_server

from .controller import Controller

logger = logging.getLogger(__name__)


def run_as_node(config):
    async def node_keepalive():
        # NOTE: I'm not really happy with this, I'd love to be able to await Peer(node).ping()
        # and then verify the status under a timeout rather than just throw away the result and
        # rely on the connection logic
        for node_id in controller.receptor.router.get_nodes():
            await controller.ping(node_id, expected_response=False)
        absolute_call_time = (
            ((int(controller.loop.time()) + 1) // config.node_keepalive_interval) + 1
        ) * config.node_keepalive_interval
        controller.loop.call_at(absolute_call_time, controller.loop.create_task, node_keepalive())

    try:
        controller = Controller(config)
        logger.info(f"Running as Receptor node with ID: {controller.receptor.node_id}")
        if config.node_stats_enable:
            logger.info(f"Starting stats on port {config.node_stats_port}")
            start_http_server(config.node_stats_port)
        if not config.node_server_disable:
            listen_tasks = controller.enable_server(config.node_listen)
            controller.loop.create_task(controller.exit_on_exceptions_in(listen_tasks))
        for peer in config.node_peers:
            controller.add_peer(
                peer,
                ws_extra_headers=config.node_ws_extra_headers,
                ws_heartbeat=config.node_ws_heartbeat,
            )
        if config.node_keepalive_interval > 1:
            controller.loop.create_task(node_keepalive())
        controller.loop.create_task(
            controller.receptor.connection_manifest.watch_expire(controller.receptor.buffer_mgr)
        )
        controller.run()
    finally:
        controller.cleanup_tmpdir()


async def run_oneshot_command(
    controller, peer, recipient, ws_extra_headers, ws_heartbeat, send_func, read_func
):
    if (not recipient) or (recipient != controller.receptor.node_id):
        add_peer_task = controller.add_peer(
            peer, ws_extra_headers=ws_extra_headers, ws_heartbeat=ws_heartbeat
        )
        start_wait = time.time()
        while True:
            if add_peer_task and add_peer_task.done() and not add_peer_task.result():
                print("Connection failed. Exiting.")
                return False
            if (
                (not recipient or controller.receptor.router.node_is_known(recipient))
                and controller.receptor.route_send_time is not None
                and time.time() - controller.receptor.route_send_time > 2.0
            ):
                break
            if time.time() - start_wait > 10:
                print("Connection timed out. Exiting.")
                if not add_peer_task.done():
                    add_peer_task.cancel()
                return False
            await asyncio.sleep(0.5)
    read_task = controller.loop.create_task(read_func())
    await send_func()
    await read_task
    return True


def run_as_ping(config):
    def ping_iter():
        if config.ping_count:
            for x in range(config.ping_count):
                yield x
        else:
            while True:
                yield 0

    async def ping_entrypoint():
        return await run_oneshot_command(
            controller,
            config.ping_peer,
            config.ping_recipient,
            config.ping_ws_extra_headers,
            config.ping_ws_heartbeat,
            send_pings,
            read_responses,
        )

    async def read_responses():
        for _ in ping_iter():
            message = await controller.recv()
            print(message.payload.readall().decode())

    async def send_pings():
        for x in ping_iter():
            await controller.ping(config.ping_recipient)
            if x + 1 < config.ping_count:
                await asyncio.sleep(config.ping_delay)

    try:
        logger.info(f"Sending ping to {config.ping_recipient} via {config.ping_peer}.")
        controller = Controller(config)
        controller.run(ping_entrypoint)
    finally:
        controller.cleanup_tmpdir()


def run_as_send(config):
    async def send_entrypoint():
        return await run_oneshot_command(
            controller,
            config.send_peer,
            config.send_recipient,
            config.send_ws_extra_headers,
            config.send_ws_heartbeat,
            send_message,
            read_responses,
        )

    async def send_message():
        if config.send_payload == "-":
            data = sys.stdin.buffer.read()
        else:
            data = config.send_payload
        await controller.send(
            payload=data, recipient=config.send_recipient, directive=config.send_directive
        )

    async def read_responses():
        while True:
            message = await controller.recv()
            logger.debug(f"{message}")
            if message.header.get("in_response_to", None):
                logger.debug("Received response message")
                if message.header.get("eof", False):
                    logger.info("Received EOF")
                    if message.header.get("code", 0) != 0:
                        logger.error(f"EOF was an error result")
                        if message.payload:
                            print(f"ERROR: {message.payload.readall().decode()}")
                        else:
                            print(f"No EOF Error Payload")
                    break
                elif message.payload:
                    print(message.payload.readall().decode())
                else:
                    print("---")
            else:
                logger.warning(f"Received unknown message {message}")

    try:
        logger.info(
            f"""Sending directive {config.send_directive} to {config.send_recipient}
                via {config.send_peer}"""
        )
        controller = Controller(config)
        controller.run(send_entrypoint)
    finally:
        controller.cleanup_tmpdir()


def run_as_status(config):
    async def status_entrypoint():
        return await run_oneshot_command(
            controller,
            config.status_peer,
            None,
            config.status_ws_extra_headers,
            config.status_ws_heartbeat,
            print_status,
            noop,
        )

    async def print_status():

        # This output should be formatted so as to be parseable as YAML

        r = controller.receptor
        print("Nodes:")
        print("  Myself:", r.router.node_id)
        print("  Others:")
        for node in r.router.get_nodes():
            print("  -", node)
        print()
        print("Route Map:")
        for edge in r.router.get_edges():
            print("-", str(tuple(edge)))
        print()
        print("Known Node Capabilities:")
        for node, node_data in r.known_nodes.items():
            print("  ", node, ":", sep="")
            for cap, cap_value in node_data["capabilities"].items():
                print("    ", cap, ": ", str(cap_value), sep="")

    async def noop():
        return

    try:
        controller = Controller(config)
        controller.run(status_entrypoint)
    finally:
        controller.cleanup_tmpdir()
