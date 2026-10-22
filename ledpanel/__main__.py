"""Command line: python -m ledpanel <command> ..."""

from __future__ import annotations

import argparse
import asyncio
import logging

from . import LedPanel, discover


def _rgb(s: str) -> tuple[int, int, int]:
    s = s.lstrip("#")
    return int(s[0:2], 16), int(s[2:4], 16), int(s[4:6], 16)


async def _run(args: argparse.Namespace) -> None:
    if args.cmd == "scan":
        for d in await discover():
            print(d.address, d.name)
        return
    async with LedPanel(args.address, pin=args.pin) as led:
        print(f"panel {led.info.width}x{led.info.height} (type 0x{led.info.type_byte:02x})")
        if args.cmd == "on":
            await led.power(True)
        elif args.cmd == "off":
            await led.power(False)
        elif args.cmd == "brightness":
            await led.brightness(args.level)
        elif args.cmd == "clock":
            await led.clock(args.style, hour_24=not args.h12, show_date=args.date)
        elif args.cmd == "text":
            await led.text(args.text, color=_rgb(args.color), effect=args.effect,
                           speed=args.speed, font=args.font)
        elif args.cmd == "image":
            await led.image(args.path)
        elif args.cmd == "gif":
            await led.gif(args.path)
        elif args.cmd == "clear":
            await led.clear()
        elif args.cmd == "score":
            await led.scoreboard(args.left, args.right)
        elif args.cmd == "info":
            print("raw device info:", led.info.raw.hex())


def main() -> None:
    ap = argparse.ArgumentParser(prog="ledpanel")
    ap.add_argument("-v", "--verbose", action="store_true")
    sub = ap.add_subparsers(dest="cmd", required=True)
    sub.add_parser("scan")

    def dev(name: str) -> argparse.ArgumentParser:
        sp = sub.add_parser(name)
        sp.add_argument("address")
        sp.add_argument("--pin")
        return sp

    dev("info"), dev("on"), dev("off"), dev("clear")
    dev("brightness").add_argument("level", type=int)
    c = dev("clock")
    c.add_argument("--style", type=int, default=1)
    c.add_argument("--h12", action="store_true")
    c.add_argument("--date", action="store_true")
    t = dev("text")
    t.add_argument("text")
    t.add_argument("--color", default="ffffff")
    t.add_argument("--effect", type=int, default=0)
    t.add_argument("--speed", type=int, default=50)
    t.add_argument("--font")
    dev("image").add_argument("path")
    dev("gif").add_argument("path")
    s = dev("score")
    s.add_argument("left", type=int)
    s.add_argument("right", type=int)

    args = ap.parse_args()
    logging.basicConfig(level=logging.DEBUG if args.verbose else logging.WARNING)
    asyncio.run(_run(args))


if __name__ == "__main__":
    main()
