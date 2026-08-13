# 流式事件总线：把后台 graph 线程产生的 LLM token / 节点事件实时转发给 SSE 线程。
import queue
from contextvars import ContextVar

# 当前请求绑定的流式总线，LLM 回调在后台线程通过它推送增量事件
_STREAM_BUS: ContextVar = ContextVar("stream_bus", default=None)


class StreamBus:
    """每个 /api/chat 请求一个实例：graph 线程 emit，SSE 线程 iter_events。"""

    def __init__(self):
        self._queue: "queue.Queue[dict | None]" = queue.Queue()
        self._closed = False
        # 请求内是否已发过 Advisor 节点标签（token handler 为全局复用，标志必须按请求隔离）
        self.advisor_label_sent = False

    def emit(self, event: dict):
        # 请求结束后丢弃新事件，避免客户端断开后无限堆积
        if not self._closed:
            self._queue.put(event)

    def emit_token(self, scope: str, text: str, stream_id: str = "", live: bool = True):
        # 推送一段 LLM 输出增量；scope 区分思考过程与最终回答；
        # stream_id 用于上下文回收；live=False 表示重放流（已生成完），前端节流逐字展示
        if text:
            event = {"type": "token", "scope": scope, "text": text, "live": live}
            if stream_id:
                event["stream_id"] = stream_id
            self.emit(event)

    def iter_events(self):
        # 逐条取出事件；收到 None 哨兵表示 graph 线程结束
        while True:
            event = self._queue.get()
            if event is None:
                return
            yield event

    def close(self):
        # 通知 SSE 线程结束，并停止接收后续事件
        self._closed = True
        self._queue.put(None)


def get_stream_bus():
    # 读取当前请求的流式总线（后台线程由 bind_stream_bus 绑定）
    return _STREAM_BUS.get()


def bind_stream_bus(bus):
    # 在后台 graph 线程开头绑定当前请求的流式总线
    _STREAM_BUS.set(bus)
