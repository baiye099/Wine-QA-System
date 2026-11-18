"""命令行入口：python -m qa '问题' 或 python -m qa --interactive。"""

import argparse
import json
import sys

from ..bootstrap import build_service
from ..core.config import Settings
from ..core.errors import QAError
from ..models.conversation import ConversationState


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("question", nargs="?")
    parser.add_argument("--interactive", action="store_true", help="连续问答；:new 清空会话，:quit 退出")
    parser.add_argument("--backend", choices=["demo", "database"], help="显式覆盖运行模式")
    parser.add_argument("--json", action="store_true", help="返回结构化 QAResponse JSON")
    args = parser.parse_args()
    if not args.question and not args.interactive:
        parser.error("请输入问题或使用 --interactive")
    try:
        settings = Settings.from_env()
        if args.backend:
            settings = settings.model_copy(update={"backend": args.backend})
        service = build_service(settings)
    except QAError as exc:
        print(str(exc), file=sys.stderr)
        return 1
    state = ConversationState()

    def ask(question: str) -> int:
        response = service.ask(question, state)
        if args.json:
            print(response.model_dump_json(indent=2))
        else:
            print(response.text)
            for notice in response.notices:
                print("说明：" + notice)
            if response.sources:
                print("\n来源：")
                for source in response.sources:
                    print(f"[{source.source_id}] {source.title}" + (f" {source.url}" if source.url else ""))
        return 1 if response.status == "error" else 0

    try:
        if args.question:
            return ask(args.question)
        print("输入问题；:new 开启新会话，:quit 退出。")
        while True:
            try:
                question = input("\n你：").strip()
            except (EOFError, KeyboardInterrupt):
                return 0
            if question == ":quit":
                return 0
            if question == ":new":
                state.reset()
                print("已开启新会话。")
            elif question:
                ask(question)
    finally:
        service.close()
