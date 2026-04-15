import asyncio
import sys

from pydantic import ValidationError

from assistant.main import main

if __name__ == "__main__":
    try:
        asyncio.run(main())
    except ValidationError as e:
        print("Config error:", e, file=sys.stderr)
        sys.exit(2)
