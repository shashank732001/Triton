"""Offline test of brain/max.py against a throwaway localhost server. Run: python tests/test_max_brain.py"""
import json
import sys
import tempfile
import threading
import time
import unittest
from http.server import BaseHTTPRequestHandler, HTTPServer
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))
from brain.max import MaxBrain  # noqa: E402

TOKEN = "abc123"


class Fake(BaseHTTPRequestHandler):
    delay = 0

    def log_message(self, *a):
        pass

    def do_POST(self):
        body = json.loads(self.rfile.read(int(self.headers["Content-Length"])))
        if self.path != "/ask" or self.headers.get("Authorization") != f"Bearer {TOKEN}":
            code, out = 401, {"error": "bad token"}
        else:
            time.sleep(Fake.delay)
            code, out = 200, {"answer": "Echo: " + body["text"]}
        data = json.dumps(out).encode()
        self.send_response(code)
        self.send_header("Content-Length", str(len(data)))
        self.end_headers()
        self.wfile.write(data)


class MaxBrainTest(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        cls.srv = HTTPServer(("127.0.0.1", 0), Fake)
        threading.Thread(target=cls.srv.serve_forever, daemon=True).start()
        cls.url = f"http://127.0.0.1:{cls.srv.server_port}"
        cls.tmp = tempfile.TemporaryDirectory()
        cls.tok = Path(cls.tmp.name) / "ask.token"

    @classmethod
    def tearDownClass(cls):
        cls.srv.shutdown()
        cls.tmp.cleanup()

    def setUp(self):
        Fake.delay = 0
        self.tok.write_text(TOKEN + "\n")

    def test_success(self):
        self.assertEqual(MaxBrain(self.url, self.tok, 5).ask("why is the sky blue"), "Echo: why is the sky blue")

    def test_bad_token(self):
        self.tok.write_text("wrong")
        self.assertIsNone(MaxBrain(self.url, self.tok, 5).ask("hello there"))

    def test_timeout(self):
        Fake.delay = 1.5
        self.assertIsNone(MaxBrain(self.url, self.tok, 0.3).ask("hello there"))

    def test_server_down(self):
        s = HTTPServer(("127.0.0.1", 0), Fake)
        port = s.server_port
        s.server_close()
        m = MaxBrain(f"http://127.0.0.1:{port}", self.tok, 2)
        self.assertIsNone(m.ask("hello there"))
        self.assertFalse(m.available())           # skipped for a while now

    def test_missing_token_file(self):
        m = MaxBrain(self.url, Path(self.tmp.name) / "nope", 2)
        self.assertFalse(m.available())
        self.assertIsNone(m.ask("hello there"))

    def test_question_gate(self):
        self.assertTrue(MaxBrain.is_question("why is the sky blue"))
        self.assertFalse(MaxBrain.is_question("open netflix and play something"))
        self.assertFalse(MaxBrain.is_question("what is playing on screen"))


if __name__ == "__main__":
    unittest.main()
