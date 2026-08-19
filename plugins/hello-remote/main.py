"""hello-remote — 跨语言插件示例：以独立进程 + stdio 逐行 JSON 协议接入 Caspian。

机制对语言透明：entry.command 换成 node/ruby/编译二进制同样成立，
只要进程遵守协议（stdout 只走协议消息、stderr 可自由日志）。

启用方式: extensions_config.json 的 plugins 段添加
    "hello-remote": {"enabled": true}
"""

import json
import sys

sys.stdout.reconfigure(encoding="utf-8")
sys.stdin.reconfigure(encoding="utf-8")


def send(message):
    print(json.dumps(message, ensure_ascii=False), flush=True)


DECLARATIONS = [
    {
        "interface": "tool",
        "tool": {
            "name": "greet_remote",
            "description": "向指定名字问好（来自远程插件进程）。",
            "parameters": {
                "type": "object",
                "properties": {"name": {"type": "string", "description": "要问好的名字"}},
                "required": ["name"],
            },
        },
    },
    {"interface": "before_model"},
]


def main():
    for line in sys.stdin:
        message = json.loads(line)
        kind = message.get("type")
        if kind == "init":
            # 握手: 声明本插件提供的接口实现（配置在 message["config"]，由插件自行解释）
            send({
                "type": "declarations",
                "display_name": "hello-remote",
                "version": "0.1.0",
                "requires": [],
                "implementations": DECLARATIONS,
            })
        elif kind == "call":
            interface = message.get("interface")
            value = message.get("value") or {}
            if interface == "tool" and value.get("name") == "greet_remote":
                name = value.get("arguments", {}).get("name", "")
                send({"type": "result", "id": message["id"], "value": f"你好，{name}！来自 hello-remote 进程。"})
            elif interface == "before_model":
                # 可修改链示例: 消息末尾追加一条进程侧提示
                messages = list(value.get("messages", []))
                messages.append({"type": "human", "content": "[hello-remote 注入了上下文提示]"})
                send({"type": "result", "id": message["id"], "value": {"messages": messages}})
            else:
                send({"type": "result", "id": message["id"], "value": value})
        elif kind == "shutdown":
            break


if __name__ == "__main__":
    main()
