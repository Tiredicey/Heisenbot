import argparse
import asyncio


def main():
    p = argparse.ArgumentParser(prog="heisenbot", description="Walter White reaction bot for a Messenger group chat")
    sub = p.add_subparsers(dest="cmd")
    s = sub.add_parser("ui", help="open the control room (default)")
    s.add_argument("--port", type=int, default=8765)
    s.add_argument("--no-open", action="store_true")
    sub.add_parser("run", help="listen headless using saved settings, no dashboard")
    t = sub.add_parser("test", help="simulate a chat message and render the reply clip")
    t.add_argument("text")
    t.add_argument("--sender", default="Francis Gerald")
    st = sub.add_parser("starter", help="build starter clips from one picture of your model")
    st.add_argument("image")
    a = p.parse_args()

    if a.cmd in (None, "ui"):
        from .server import serve

        serve(port=getattr(a, "port", 8765), open_browser=not getattr(a, "no_open", False))
    elif a.cmd == "run":
        from .bot import Bot

        async def go():
            bot = Bot()
            await bot.start()
            await bot.task

        asyncio.run(go())
    elif a.cmd == "test":
        from .bot import Bot

        res = asyncio.run(Bot().test(a.sender, a.text))
        print(res or "No reaction for that message.")
    elif a.cmd == "starter":
        from .config import CLIPS
        from .media import make_starter_clips

        for m in make_starter_clips(a.image, CLIPS, overwrite=True):
            print(m)


if __name__ == "__main__":
    main()
