from __future__ import annotations

import argparse
import json
from .agent import AgentCore
from .capabilities import collect_capabilities
from .config import load_config, merge_config, save_config
from .telegram_bot import TelegramBot
from .updater import update_system


def main(argv: list[str] | None = None) -> None:
    parser = argparse.ArgumentParser(prog="nermana")
    sub = parser.add_subparsers(dest="command", required=True)
    serve = sub.add_parser("serve")
    serve.add_argument("--host")
    serve.add_argument("--port", type=int)
    chat = sub.add_parser("chat")
    chat.add_argument("message", nargs="*")
    chat.add_argument("--session", default="cli")
    sub.add_parser("doctor")
    sub.add_parser("models")
    tools = sub.add_parser("tools")
    tools.add_argument("--run")
    tools.add_argument("--payload", default="{}")
    memory = sub.add_parser("memory")
    memory.add_argument("--search")
    memory.add_argument("--add")
    telegram = sub.add_parser("telegram")
    telegram.add_argument("--once", action="store_true")
    sub.add_parser("start")
    sub.add_parser("update")
    args = parser.parse_args(argv)

    if args.command == "serve":
        _serve(args)
        return
    if args.command == "start":
        from .startup import main as startup_main

        startup_main()
        return
    if args.command == "update":
        print(json.dumps(update_system(), indent=2))
        return

    agent = AgentCore()
    if args.command == "chat":
        message = " ".join(args.message).strip() or input("You: ")
        print(agent.chat(message, session_id=args.session)["reply"])
    elif args.command == "doctor":
        caps = collect_capabilities(agent.config, agent.models, agent.tools)
        print(json.dumps([cap.__dict__ for cap in caps], indent=2))
    elif args.command == "models":
        print(json.dumps([model.__dict__ for model in agent.models.scan()], indent=2))
    elif args.command == "tools":
        if args.run:
            print(json.dumps(agent.run_tool(args.run, json.loads(args.payload)), indent=2))
        else:
            print(json.dumps(agent.tools.list_metadata(), indent=2))
    elif args.command == "memory":
        if args.add:
            print(agent.memory.remember(args.add))
        elif args.search:
            print(json.dumps([hit.__dict__ for hit in agent.memory.search(args.search)], indent=2))
        else:
            print(json.dumps(agent.memory.list_memories(), indent=2))
    elif args.command == "telegram":
        bot = TelegramBot(agent)
        if args.once:
            print(json.dumps(bot.poll_once(), indent=2))
        else:
            bot.run_forever()


def _serve(args: argparse.Namespace) -> None:
    cfg = load_config()
    patch = {"server": {}}
    if args.host:
        patch["server"]["host"] = args.host
    if args.port:
        patch["server"]["port"] = args.port
    if patch["server"]:
        cfg = merge_config(cfg, patch)
        save_config(cfg)
    from .simple_server import serve

    serve(cfg.server.host, cfg.server.port)


if __name__ == "__main__":
    main()
