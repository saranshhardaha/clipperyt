"""Appended to the test copy of chat_youtube.py / chat_twitch.py: synthetic live chat with one burst."""
def _fake_main():
    import sys, time, random
    out, t0, n = sys.argv[2], time.time(), 0
    try:
        with open(out, "a", encoding="utf-8", buffering=1) as f:
            while True:
                t = time.time() - t0
                burst = 62 <= t < 68
                for _ in range(8 if burst else 1):
                    n += 1
                    f.write(f"{int(time.time()*1000)}\tuser{random.randint(1, 60)}\t{'KEKW OMG' if burst else 'msg %d' % n}\n")
                time.sleep(0.5)
    except KeyboardInterrupt:
        pass
if __name__ == "__main__":
    _fake_main()
