# -*- coding: utf-8 -*-
"""M79 冒烟实例：临时数据目录 + 脚本化 LLM，造出三种可见性状态。

不碰生产 proto_data；端口由系统分配；结束后自动退出。
状态：① 聊天历史（2 轮）② 已完成带 note 的执行链 ③ 暂停舰队的排队续跑。
"""
import json
import sys
import threading
import tempfile
import urllib.request
from pathlib import Path

from proto.llm.client import LLMClient
from proto.runtime.agent import ProtoAgent
from proto.web.server import ProtoWebServer


class SmokeLLM(LLMClient):
    def __init__(self):
        self.loop_steps = 0

    def complete(self, system, user):
        if "独立验收" in system:
            return json.dumps({"verdict": "pass"})
        if "最小可检验定义" in system:
            return json.dumps({"acceptance": "clock 返回当前时间"})
        if "任务执行循环" in system:
            self.loop_steps += 1
            if self.loop_steps == 1:
                return json.dumps({"tool": "clock", "args": {}, "category": "read_draft",
                                   "note": "读一次时钟，确认工具链可用"})
            if self.loop_steps == 2:
                return json.dumps({"tool": "clock", "args": {}, "category": "read_draft",
                                   "note": "再读一次时钟核对秒级精度"})
            return json.dumps({"done": True, "summary": "时钟工具链验证通过，两次读数一致"})
        if "归因" in system:
            return json.dumps([{"param": "instincts.social_initiative",
                                "direction": 1, "strength": 0.6}])
        return "好的主人，我在。"


def call(srv, method, path, body=None):
    req = urllib.request.Request(
        f"http://127.0.0.1:{srv.port}{path}",
        data=json.dumps(body).encode() if body else None, method=method,
        headers={"X-Proto-Token": srv.token, "Content-Type": "application/json"})
    with urllib.request.urlopen(req, timeout=10) as r:
        return json.loads(r.read())


def main():
    tmp = Path(tempfile.mkdtemp(prefix="proto-m79-smoke-"))
    print(f"[smoke] data dir: {tmp}", flush=True)
    agent = ProtoAgent(data_dir=tmp / "data", llm=SmokeLLM(), goal_interval_s=0)
    srv = ProtoWebServer(agent, host="127.0.0.1", port=0, heartbeat_s=0)
    threading.Thread(target=srv.serve_forever, daemon=True).start()
    print(f"[smoke] url: http://127.0.0.1:{srv.port}/#token={srv.token}", flush=True)

    aid = srv.manager.current_id()
    runs = srv.manager.runs

    # ① 聊天历史两轮
    for text in ("你好，冒烟测试", "刚才做了什么"):
        call(srv, "POST", "/api/chat", {"text": text})

    # ② 完整执行链（带 note 的两步 + done）
    done = runs.enqueue(aid, "task", {"text": "验证时钟工具链"})
    final = runs.wait(done["id"], timeout=15)
    print(f"[smoke] task status: {final['status']}", flush=True)

    # ③ 暂停舰队 + 排队续跑（伪装 model_retry_wait 的真实形态）
    srv.manager.pause(aid, True)
    queued = runs.enqueue(aid, "tick", {})
    with runs.database.connect(write=True) as connection:
        connection.execute(
            "UPDATE runs SET result = ?, session_id = ? WHERE id = ?",
            (json.dumps({"resume_session": True, "status": "waiting_model",
                         "model_reason": "model_rate_limited",
                         "retry_at": 1795360487.4}),
             final["session_id"], queued["id"]))
    print(f"[smoke] queued continuation: {queued['id']} blocked="
          f"{runs.blocked_reason(runs.get(queued['id']))}", flush=True)

    # 自检：三个端点都该有料
    history = call(srv, "GET", "/api/chat/history?n=10")
    print(f"[smoke] history turns: {len(history['history'])}", flush=True)
    timeline = call(srv, "GET", f"/api/runs/{done['id']}/timeline")
    print(f"[smoke] timeline steps: {len(timeline['steps'])} notes="
          f"{[s['note'] for s in timeline['steps']]}", flush=True)
    live = call(srv, "GET", "/api/activity/live")
    print(f"[smoke] live blocked={live.get('blocked')} run={live['run']['id']}", flush=True)

    print("[smoke] READY — 截图后 Ctrl-C 或 kill 退出", flush=True)
    threading.Event().wait(600)


if __name__ == "__main__":
    sys.exit(main())
