"""Process entry points. Credentials arrive via environment, never CLI arguments."""

import asyncio
import json
import os
import sys
from contextlib import suppress
from pathlib import Path

from .core import DOMAIN, EventLog, jid, write_json


def create_local_certificate(directory: Path):
    # PyJabber's generator expects a bundled CA in a custom certificate directory.
    # Generate this session's own self-signed certificate instead of copying CA keys.
    import ipaddress
    from datetime import datetime, timedelta, timezone

    from cryptography import x509
    from cryptography.hazmat.primitives import hashes, serialization
    from cryptography.hazmat.primitives.asymmetric import rsa
    from cryptography.x509.oid import NameOID

    key_path = directory / f"{DOMAIN}_key.pem"
    cert_path = directory / f"{DOMAIN}_cert.pem"
    if key_path.exists() and cert_path.exists():
        return
    key = rsa.generate_private_key(public_exponent=65537, key_size=2048)
    name = x509.Name([x509.NameAttribute(NameOID.COMMON_NAME, DOMAIN)])
    now = datetime.now(timezone.utc)
    certificate = (
        x509.CertificateBuilder()
        .subject_name(name)
        .issuer_name(name)
        .public_key(key.public_key())
        .serial_number(x509.random_serial_number())
        .not_valid_before(now - timedelta(minutes=1))
        .not_valid_after(now + timedelta(days=7))
        .add_extension(
            x509.SubjectAlternativeName([x509.IPAddress(ipaddress.ip_address(DOMAIN))]),
            critical=False,
        )
        .sign(key, hashes.SHA256())
    )
    key_path.write_bytes(
        key.private_bytes(
            serialization.Encoding.PEM,
            serialization.PrivateFormat.PKCS8,
            serialization.NoEncryption(),
        )
    )
    cert_path.write_bytes(certificate.public_bytes(serialization.Encoding.PEM))


async def wait_for_stop(path: Path):
    while not path.exists():
        await asyncio.sleep(0.1)


async def monitor_presence(directory: Path):
    from pyjabber.features.presence.PresenceFeature import Presence
    from pyjabber.network.ConnectionManager import ConnectionManager
    from pyjabber.stream.JID import JID

    log = EventLog(directory, "xmpp")
    previous = None
    while True:
        state = {}
        for role in ("k1315", "k1316", "coordinator"):
            identity = JID(jid(role))
            state[role] = {
                "connections": [
                    {"jid": str(client.jid), "online": client.online}
                    for client in ConnectionManager().get_transport(identity)
                ],
                "presence": [
                    [getattr(value, "value", value) for value in item]
                    for item in Presence().priority_by_jid(identity)
                ],
            }
        if state != previous:
            log.write("presence_changed", state=state)
            previous = state
        await asyncio.sleep(0.5)


def mark_ready(directory: Path, role: str, **details):
    write_json(directory / f"{role}.ready.json", {"pid": os.getpid(), **details})


async def serve_xmpp(args):
    from loguru import logger
    from pyjabber import AppConfig
    from pyjabber.plugins.xep_0363.upload_server import UploadHttpServer
    from pyjabber.plugins.xep_0363.xep_0363 import HTTPFieldUpload
    from pyjabber.server import Server
    from pyjabber.server_parameters import Parameters

    logger.remove()
    logger.add(sys.stderr, level="WARNING")
    certificates = args.run_dir / "certificates"
    certificates.mkdir(exist_ok=True)
    create_local_certificate(certificates)
    server = Server(
        Parameters(
            host=DOMAIN,
            client_port=args.port,
            server_port=args.s2s_port,
            cert_path=str(certificates),
            database_in_memory=True,
            message_persistence=False,
            plugins=["jabber:iq:register", "urn:xmpp:ping"],
            items={
                "pubsub.$": {
                    "name": "Local PubSub",
                    "category": "pubsub",
                    "type": "service",
                    "var": "http://jabber.org/protocol/pubsub",
                    "extra": {},
                },
                "upload.$": {"extra": {"max-size": 0}},
            },
        )
    )
    # The pinned PluginManager constructs HTTPFieldUpload() unconditionally.
    # Initialize its singleton without starting the optional HTTP listener.
    HTTPFieldUpload(UploadHttpServer())
    # PyJabber 0.4.5 also binds its detected LAN IP even when host is loopback.
    # Keep this version-specific adapter here; no installed dependency is edited.
    server._public_ip = None
    task = asyncio.create_task(server.start())
    ready = asyncio.create_task(server.ready.wait())
    try:
        done, _ = await asyncio.wait([task, ready], timeout=15, return_when=asyncio.FIRST_COMPLETED)
        if ready not in done:
            if task.done():
                await task
            raise RuntimeError("XMPP startup failed or timed out")
        listeners = [
            socket.getsockname()
            for listener in (server._client_listener, server._server_listener)
            for socket in listener.sockets
        ]
        if any(address[0] != DOMAIN for address in listeners):
            raise RuntimeError("XMPP listener escaped loopback")
        mark_ready(
            args.run_dir,
            "xmpp",
            host=DOMAIN,
            port=args.port,
            s2s_port=args.s2s_port,
            listeners=listeners,
        )
        stop = asyncio.create_task(wait_for_stop(args.run_dir / "xmpp.stop"))
        monitor = asyncio.create_task(monitor_presence(args.run_dir))
        try:
            done, _ = await asyncio.wait([task, stop, monitor], return_when=asyncio.FIRST_COMPLETED)
            if monitor in done:
                await monitor
            if task in done:
                await task
                raise RuntimeError("XMPP service stopped unexpectedly")
        finally:
            stop.cancel()
            monitor.cancel()
            await asyncio.gather(stop, monitor, return_exceptions=True)
    finally:
        ready.cancel()
        task.cancel()
        await asyncio.gather(task, ready, return_exceptions=True)
        AppConfig.app_config.process_pool_exe.shutdown(wait=True, cancel_futures=True)


async def coordinator_console(agent):
    """JSON-lines local control channel; all room operations travel over XMPP."""
    pending = set()

    async def respond(command):
        request_id = command.get("request_id")
        try:
            if command.get("command") == "ledger":
                value = agent.ledger()
            elif command.get("command") == "tasks":
                tasks = command.get("tasks")
                if not isinstance(tasks, list) or not 1 <= len(tasks) <= 50:
                    raise ValueError("tasks must contain between 1 and 50 requests")
                allowed = {"room_id", "payload", "timeout", "repeats", "task_id"}
                if any(
                    not isinstance(item, dict)
                    or not {"room_id", "payload"} <= item.keys()
                    or not item.keys() <= allowed
                    for item in tasks
                ):
                    raise ValueError("invalid task fields in batch")
                # One malformed request must not orphan the rest of a batch.
                results = await asyncio.gather(
                    *(agent.request(**item) for item in tasks), return_exceptions=True
                )
                value = [
                    {"status": "failed", "reason": str(item)}
                    if isinstance(item, Exception)
                    else item
                    for item in results
                ]
            else:
                raise ValueError("command must be tasks or ledger")
            reply = {"request_id": request_id, "ok": True, "data": value}
        except (ValueError, TypeError) as exc:
            reply = {"request_id": request_id, "ok": False, "error": str(exc)}
        print(json.dumps(reply, allow_nan=False), flush=True)

    try:
        while True:
            line = await asyncio.to_thread(sys.stdin.readline)
            if not line:
                break
            try:
                command = json.loads(line)
                if not isinstance(command, dict):
                    raise ValueError("command must be an object")
            except (ValueError, TypeError) as exc:
                print(json.dumps({"request_id": None, "ok": False, "error": str(exc)}), flush=True)
                continue
            task = asyncio.create_task(respond(command))
            pending.add(task)
            task.add_done_callback(pending.discard)
    finally:
        if pending:
            await asyncio.gather(*pending)


async def serve_agent(args):
    from .agents import CoordinatorAgent, RoomAgent

    password = os.environ.get("OFFICE_SPADE_PASSWORD")
    if not password:
        raise ValueError("set OFFICE_SPADE_PASSWORD for this local test session")
    role = "coordinator" if args.role == "coordinator" else args.room.lower()
    log = EventLog(args.run_dir, role)
    state = None
    if args.role == "room" and os.getenv("OFFICE_ROOM_BACKEND") == "building":
        from .building_room import BuildingRoomState

        state = BuildingRoomState(args.room, log)
    agent = (
        CoordinatorAgent(password, args.port, log)
        if args.role == "coordinator"
        else RoomAgent(args.room, password, args.port, log, state=state)
    )
    console = None
    stopper = None
    try:
        await asyncio.wait_for(agent.start(auto_register=True), timeout=15)
        log.write(
            "agent_ready",
            room_id=args.room if args.role == "room" else None,
            session_started=agent.client._session_started,
            outgoing_queue=agent.client.waiting_queue.qsize(),
        )
        agent.presence.set_available()
        mark_ready(args.run_dir, role)
        stopper = asyncio.create_task(wait_for_stop(args.run_dir / f"{role}.stop"))
        if args.role == "coordinator":
            console = asyncio.create_task(coordinator_console(agent))
            await asyncio.wait([console, stopper], return_when=asyncio.FIRST_COMPLETED)
            if console.done():
                await console
        else:
            await stopper
    finally:
        if stopper is not None:
            stopper.cancel()
        if console is not None and not console.done():
            console.cancel()
            with suppress(asyncio.CancelledError):
                await console
        if agent.is_alive():
            await asyncio.wait_for(agent.stop(), timeout=5)
        log.write("agent_stopped")


def run_worker(args):
    if args.role == "building":
        from .building import serve_building

        args.run_dir = args.run_dir.resolve()
        serve_building(args)
        return
    import spade  # Imports may choose a Windows event loop policy.

    args.run_dir = args.run_dir.resolve()
    args.run_dir.mkdir(parents=True, exist_ok=True)
    if args.role == "xmpp":
        asyncio.run(serve_xmpp(args))
    else:
        # Use SPADE's own container loop; each worker contains exactly one Agent.
        import traceback

        failures = []

        async def guarded():
            try:
                await serve_agent(args)
            except Exception as exc:
                traceback.print_exc()
                failures.append(exc)

        # SPADE's convenience runner logs and swallows exceptions. Preserve a
        # nonzero worker exit status so startup failure cannot look successful.
        spade.run(guarded(), embedded_xmpp_server=False)
        if failures:
            raise SystemExit(1)
