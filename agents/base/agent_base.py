import json
import sys

class BaseAgent:

    def run(self):
        task = json.load(sys.stdin)
        result = self.handle(task)
        print(json.dumps(result))

    def handle(self, task):
        raise NotImplementedError