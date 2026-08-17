import uvicorn

from jo_web.app import build_app
from jo_web.config import load_web_config


def main():
    config = load_web_config()
    uvicorn.run(build_app(config), host=config.host, port=config.port, log_level=config.log_level.lower())


if __name__ == "__main__":
    main()
