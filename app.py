import asyncio, os
from main import main

if __name__ == "__main__":
    os.environ["PORT"] = os.environ.get("PORT","5000")
    asyncio.run(main())
