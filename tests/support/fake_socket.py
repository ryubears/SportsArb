"""
A websocket stand in, for tests that drive a stream's subscribe and send_command by hand.
"""

import json


class Socket:
    """
    Keeps every frame sent, decoded from JSON.
    """

    def __init__(self):
        self.sent = []

    async def send(self, frame):
        self.sent.append(json.loads(frame))
