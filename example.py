# Run with python3 example.py -h for help
"""aioshelly usage example."""
from __future__ import annotations

import argparse
import asyncio
import json
import logging
import signal
import sys
import traceback
from datetime import datetime
from types import FrameType
from typing import Any, Tuple, cast

import aiohttp
import async_timeout

import aioshelly
from aioshelly.block_device import BLOCK_VALUE_UNIT, COAP, BlockDevice
from aioshelly.common import ConnectionOptions
from aioshelly.const import MODEL_NAMES
from aioshelly.exceptions import ShellyError, WrongShellyGen
from aioshelly.rpc_device import RpcDevice


async def get_coap_context(port: int) -> COAP:
    """Create CoAP context"""
    context = COAP()

    def handle_sigint(_exit_code: int, _frame: FrameType) -> None:
        """Handle Keyboard signal interrupt (ctrl-c)."""
        context.close()
        sys.exit()

    signal.signal(signal.SIGINT, handle_sigint)

    await context.initialize(port)
    return context


async def create_device(  # pylint: disable=too-many-arguments
    aiohttp_session: aiohttp.ClientSession,
    coap_context: COAP,
    options: ConnectionOptions,
    init: bool,
    timeout: float,
    gen: int | None,
) -> Any:
    """Create a Gen1/Gen2 device"""
    if gen is None:
        async with async_timeout.timeout(timeout):
            if info := await aioshelly.common.get_info(
                aiohttp_session, options.ip_address
            ):
                gen = info.get("gen", 1)
            else:
                raise ShellyError("Unknown Gen")

    if gen == 1:
        return await BlockDevice.create(aiohttp_session, coap_context, options, init)

    if gen == 2:
        return await RpcDevice.create(aiohttp_session, options, init)

    raise ShellyError("Unknown Gen")


async def test_single(
    options: ConnectionOptions,
    init: bool,
    timeout: float,
    port: int,
    gen: int | None,
) -> None:
    """Test single device."""
    async with aiohttp.ClientSession() as aiohttp_session:
        try:
            coap_context = await get_coap_context(port)
            async with async_timeout.timeout(timeout):
                device = await create_device(
                    aiohttp_session, coap_context, options, init, timeout, gen
                )
        except asyncio.TimeoutError:
            print("Timeout connecting to", options.ip_address)
            return
        except WrongShellyGen:
            print(f"Wrong Shelly generation {gen}, device gen: {2 if gen==1 else 1}")
            return

        print_device(device)

        device.subscribe_updates(device_updated)

        while True:
            await asyncio.sleep(0.1)


async def test_devices(init: bool, timeout: float, port: int, gen: int | None) -> None:
    """Test multiple devices."""
    device_options = []
    with open("devices.json") as fp:
        for line in fp:
            device_options.append(ConnectionOptions(**json.loads(line)))

    async with aiohttp.ClientSession() as aiohttp_session:
        coap_context = await get_coap_context(port)
        results = await asyncio.gather(
            *[
                asyncio.wait_for(
                    connect_and_print_device(
                        aiohttp_session, coap_context, options, init, timeout, gen
                    ),
                    timeout,
                )
                for options in device_options
            ],
            return_exceptions=True,
        )

        for options, result in zip(device_options, results):
            if not isinstance(result, Exception):
                continue

            print()
            print(f"Error printing device @ {options.ip_address}")

            if isinstance(result, asyncio.TimeoutError):
                print("Timeout connecting to device")
            else:
                print()
                traceback.print_tb(result.__traceback__)
                print(result)

        while True:
            await asyncio.sleep(0.1)


async def connect_and_print_device(  # pylint: disable=too-many-arguments
    aiohttp_session: aiohttp.ClientSession,
    coap_context: COAP,
    options: ConnectionOptions,
    init: bool,
    timeout: float,
    gen: int | None,
) -> None:
    """Connect and print device data."""
    device = await create_device(
        aiohttp_session, coap_context, options, init, timeout, gen
    )
    print_device(device)
    device.subscribe_updates(device_updated)


def device_updated(cb_device: BlockDevice | RpcDevice) -> None:
    """Device updated callback."""
    print()
    print(f"{datetime.now().strftime('%H:%M:%S')} Device updated!")
    print_device(cb_device)


def print_device(device: BlockDevice | RpcDevice) -> None:
    """Print device data."""
    if not device.initialized:
        print()
        print(f"** Device @ {device.ip_address} not initialized **")
        print()
        return

    model_name = MODEL_NAMES.get(device.model) or f"Unknown ({device.model})"
    print(f"** {model_name} - {device.hostname} @ {device.ip_address} **")
    print()

    if device.gen == 1:
        print_block_device(cast(BlockDevice, device))
    elif device.gen == 2:
        print_rpc_device(cast(RpcDevice, device))


def print_block_device(device: BlockDevice) -> None:
    """Print block (GEN1) device data."""
    assert device.blocks

    for block in device.blocks:
        print(block)
        for attr, value in block.current_values().items():
            info = block.info(attr)

            if value is None:
                value = "-"

            if BLOCK_VALUE_UNIT in info:
                unit = " " + info[BLOCK_VALUE_UNIT]
            else:
                unit = ""

            print(f"{attr.ljust(16)}{value}{unit}")
        print()


def print_rpc_device(device: RpcDevice) -> None:
    """Print RPC (GEN2) device data."""
    if device.connected:
        print(f"Status: {device.status}")
        print(f"Event: {device.event}")
    else:
        print("Device disconnected")


def get_arguments() -> Tuple[argparse.ArgumentParser, argparse.Namespace]:
    """Get parsed passed in arguments."""
    parser = argparse.ArgumentParser(description="aioshelly example")
    parser.add_argument(
        "--ip_address", "-ip", type=str, help="Test single device by IP address"
    )
    parser.add_argument(
        "--socket_port",
        "-sp",
        type=int,
        default=5683,
        help="Specify socket UDP port (default=5683)",
    )
    parser.add_argument(
        "--devices",
        "-d",
        action="store_true",
        help='Connect to all the devices in "devices.json" at once and print their status',
    )
    parser.add_argument(
        "--init", "-i", action="store_true", help="Init device(s) at startup"
    )
    parser.add_argument(
        "--timeout",
        "-t",
        type=int,
        default=5,
        help="Device init timeout in seconds (default=5)",
    )
    parser.add_argument("--username", "-u", type=str, help="Set device username")
    parser.add_argument("--password", "-p", type=str, help="Set device password")

    parser.add_argument(
        "--gen1", "-g1", action="store_true", help="Force Gen1 (CoAP) device"
    )
    parser.add_argument(
        "--gen2", "-g2", action="store_true", help="Force Gen 2 (RPC) device"
    )
    parser.add_argument(
        "--debug", "-deb", action="store_true", help="Enable debug level for logging"
    )

    arguments = parser.parse_args()

    return parser, arguments


async def main() -> None:
    """Run main."""
    parser, args = get_arguments()

    if args.gen1 and args.gen2:
        parser.error("--gen1 and --gen2 can't be used together")

    gen = None
    if args.gen1:
        gen = 1
    elif args.gen2:
        gen = 2

    if args.debug:
        logging.basicConfig(level="DEBUG", force=True)

    if args.devices:
        await test_devices(args.init, args.timeout, args.socket_port, gen)
    elif args.ip_address:
        if args.username and args.password is None:
            parser.error("--username and --password must be used together")
        options = ConnectionOptions(args.ip_address, args.username, args.password)
        await test_single(options, args.init, args.timeout, args.socket_port, gen)
    else:
        parser.error("--ip_address or --devices must be specified")


if __name__ == "__main__":
    asyncio.run(main())
