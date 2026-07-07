from agentTest.state.xcom_record import XComRecord


class AgentState:

    def __init__(self):

        # 用户输入
        self.query = ""

        # DAG Plan
        self.current_plan = []

        #XCom(跨step数据通信)
        self.xcom :dict[str, XComRecord] = {}

        # step状态，生命后期c
        self.step_status = {}

        # 执行轨迹
        self.trace = []

        #重试次数
        self.step_retry = {}

