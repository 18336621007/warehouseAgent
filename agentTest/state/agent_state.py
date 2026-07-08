from agentTest.state.xcom_record import XComRecord


class AgentState:

    def __init__(self):

        # 用户输入
        self.query = ""

        # DAG Plan
        self.current_plan = [] #当前计划
        self.xcom: dict[str, XComRecord] = {} #结果快照
        self.step_status = {} #各个步骤状态
        self.trace = [] #记录路径
        self.step_retry = {} #各个步骤的重试次数

        self.run_summary = {}




    def reset_run(self):
        """重置状态，这些是每轮对话独享的"""
        self.query = ""
        self.current_plan = []
        self.xcom = {}
        self.step_status = {}

        # 执行轨迹
        self.trace = []

        #重试次数
        self.step_retry = {}
        self.run_summary = {}



