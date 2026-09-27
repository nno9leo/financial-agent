import argparse

import uvicorn

from app.api.app import ApiDeps, create_app
from app.config import Settings
from app.providers.sec import SecProvider


class LoopbackHostError(ValueError):
    pass


def main(argv=None):
    parser = argparse.ArgumentParser()
    parser.add_argument("--host", default="127.0.0.1")
    parser.add_argument("--port", type=int, default=None)
    parser.add_argument("--no-env-file", action="store_true", help="do not load .env")
    args = parser.parse_args(argv)
    if args.host != "127.0.0.1":
        raise LoopbackHostError("The API server may listen only on 127.0.0.1.")
    settings = Settings(_env_file=None) if args.no_env_file else Settings()
    port = args.port or settings.api_port
    deps = ApiDeps(settings=settings, sec_provider_factory=lambda: SecProvider(settings))
    uvicorn.run(create_app(deps), host="127.0.0.1", port=port)


if __name__ == "__main__":
    main()
