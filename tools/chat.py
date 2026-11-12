"""Talk to the backbone in a terminal against the built-in PayPal simulator (or sandbox with credentials)."""
import asyncio, os, sys
sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))
from paypal_voice.app_factory import build_engine

async def main(lines=None):
    engine, mock = build_engine(os.environ.get("PAYPAL_ENV", "mock"))
    sid = "terminal"
    if mock:
        from paypal_voice.paypal_client import Token
        import time
        engine.session(sid).user_token = Token("A21AAmock-user", time.time() + 3600)
    src = lines or iter(lambda: input("you> "), "")
    for text in src:
        if text.startswith("#"):
            continue
        r = await engine.handle_turn(sid, text)
        print(f"you> {text}" if lines else "", end="\n" if lines else "")
        print(f"  pv> {r.speech}   [{r.capability or '-'} | {r.state}{' | ' + r.expecting if r.expecting else ''}]")
        for a in r.actions:
            print(f"      action: {a}")
    await engine.client.aclose()

if __name__ == "__main__":
    script = sys.argv[1:] and open(sys.argv[1]).read().splitlines()
    asyncio.run(main(script or None))
