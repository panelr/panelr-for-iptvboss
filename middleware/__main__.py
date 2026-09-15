import logging

from aiohttp import web

from .app import create_app
from .config import Config


def main():
    logging.basicConfig(level=logging.INFO, format="%(asctime)s %(levelname)s %(name)s: %(message)s")
    config = Config.from_env()
    web.run_app(create_app(config), host=config.listen_host, port=config.listen_port,
                access_log=None, handler_cancellation=True)


if __name__ == "__main__":
    main()
