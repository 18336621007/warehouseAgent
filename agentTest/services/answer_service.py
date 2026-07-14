class AnswerService:
    # AnswerService 负责根据执行结果生成最终用户回答：
    # - 构造 answer prompt
    # - 调用 LLM 总结 trace

    def __init__(self, llm, prompt_builder):
        # 输入：
        # - llm：模型调用入口
        # - prompt_builder：answer prompt 构造器
        # 输出：
        # - 无直接输出，answer 方法返回最终自然语言答案
        self.llm = llm
        self.prompt_builder = prompt_builder

    def answer(self, state):
        # 输入：
        # - state：当前轮次执行状态，重点使用其中的 trace
        # 输出：
        # - answer：模型生成的最终自然语言回答
        answer_messages = self.prompt_builder.build_answer_prompt(state)
        return self.llm.chat(answer_messages)
