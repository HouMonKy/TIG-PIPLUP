import json
import re
from datetime import datetime
from src.data_process.template_extractor.log_formatter_parse import LogFormatParser
from src.data_process.template_extractor.parsing_algorithm.prompts.prompts_log_format import build_prompt, base_prompt1


class LogExtractionEngine:
    def __init__(self, parser_instance: LogFormatParser, llm_client_func, log_module='osd'):
        self.parser = parser_instance
        self.llm_client = llm_client_func
        self.log_module = log_module
        self.max_retries = 3
        self.model_name = 'qwen3.6-27b'

        if self.log_module not in self.parser.log_format:
            self.parser.log_format[self.log_module] = {"log_format": []}

    @staticmethod
    def _extract_json_from_response(response: str) -> str:
        """括号平衡与正则提取算法"""
        json_match = re.search(r'```json\s*(\{.*?\})\s*```', response, re.DOTALL)
        if json_match and ('"new_pattern"' in json_match.group(1) or '"pattern"' in json_match.group(1)):
            return json_match.group(1)

        json_match = re.search(r'```\s*(\{.*?\})\s*```', response, re.DOTALL)
        if json_match and ('"new_pattern"' in json_match.group(1) or '"pattern"' in json_match.group(1)):
            return json_match.group(1)

        start = response.find('{')
        if start == -1:
            return None

        depth = 0
        end = start
        for i, c in enumerate(response[start:], start):
            if c == '{':
                depth += 1
            elif c == '}':
                depth -= 1
                if depth == 0:
                    end = i
                    break

        if depth == 0:
            json_str = response[start:end + 1]
            if '"new_pattern"' in json_str or '"pattern"' in json_str:
                return json_str
        return None

    @staticmethod
    def fix_json_backslashes(raw_str):
        """转义符修复逻辑"""
        if not raw_str:
            return raw_str

        valid_json_escapes = set('unrtbf"\\/')
        valid_regex_escapes = set('dswDSWbAZe0ntrfbua')
        fix_needed = set('[](){|')

        result = []
        i = 0
        while i < len(raw_str):
            c = raw_str[i]
            if c == '\\' and i + 1 < len(raw_str):
                next_char = raw_str[i + 1]
                if next_char in valid_json_escapes:
                    result.append(raw_str[i:i + 2])
                    i += 2
                elif next_char in valid_regex_escapes:
                    result.append(raw_str[i:i + 2])
                    i += 2
                elif next_char in fix_needed:
                    result.append('\\\\')
                    i += 2
                else:
                    result.append(raw_str[i:i + 2])
                    i += 2
            else:
                result.append(c)
                i += 1
        return ''.join(result)

    def _generate_pattern_with_reflection(self, log_line, failed_patterns):
        reflection_context = ""
        if failed_patterns:
            reflection_context = "\n【注意：以下是你之前的错误尝试，请不要重复使用，并分析其匹配失败的原因】:\n"
            for i, p in enumerate(failed_patterns):
                reflection_context += f"{i + 1}. {p}\n"

        prompt = build_prompt(log_line, reflection_context)

        try:
            response = self.llm_client(prompt, self.model_name)

            json_str = self._extract_json_from_response(response)
            if not json_str:
                raise ValueError("无法从 LLM 输出中提取 JSON 格式的数据")

            json_str = self.fix_json_backslashes(json_str)
            data = json.loads(json_str)

            new_pattern = data.get("new_pattern")
            if new_pattern:
                if r'\[' in new_pattern:
                    new_pattern = new_pattern.replace(r'\[', '__LBRACKET__').replace(r'\]', '__RBRACKET__')

            return new_pattern, response, None

        except (ValueError, json.JSONDecodeError) as e:
            return None, response if 'response' in locals() else "请求失败", str(e)
        except Exception as e:
            return None, "发生未知异常", str(e)

    def _apply_pattern_hack(self):
        """将底层编译好的正则中的 __LBRACKET__ / __RBRACKET__ 替换回真实的 \[ \] """
        if self.log_module in self.parser.format_regex:
            for i, (headers, regex) in enumerate(self.parser.format_regex[self.log_module]):
                pattern = regex.pattern
                if '__LBRACKET__' in pattern or '__RBRACKET__' in pattern:
                    pattern = pattern.replace('__LBRACKET__', r'\[')
                    pattern = pattern.replace('__RBRACKET__', r'\]')
                    try:
                        self.parser.format_regex[self.log_module][i] = (headers, re.compile(pattern))
                    except re.error:
                        pass

    def extract(self, log_line):
        """核心提取逻辑：命中缓存 -> LLM 学习 -> 更新正则 -> 重试"""

        # 1. 优先尝试现有规则 (修复你原本未缓存的问题)
        timestamp, content = self.parser.load_data(self.log_module, log_line)
        if timestamp and content:
            # 命中现有正则，直接返回，不再打印大段日志
            return timestamp, content

        # 2. 如果未命中，进入 LLM 智能解析
        print(f"[未知格式] 启动 LLM 智能解析...")
        failed_attempts = []

        for attempts in range(1, self.max_retries + 1):
            new_pattern, response, reason = self._generate_pattern_with_reflection(log_line, failed_attempts)

            if not new_pattern:
                failed_attempts.append(f"{response} \n失败原因：JSON 提取或解析异常, {reason}")
                print(f"[第 {attempts} 次尝试失败] LLM 输出格式异常: {reason}")
                continue

            print(f"[LLM 生成正则] {new_pattern}")

            # 3. 临时将新规则放到 parser 验证队列的【最前面】
            target_list = self.parser.log_format[self.log_module]["log_format"]
            target_list.insert(0, new_pattern)

            self.parser.refresh_format_regex(self.log_module)
            self._apply_pattern_hack()

            # 4. 重新调用解析器验证
            timestamp, content = self.parser.load_data(self.log_module, log_line)

            if timestamp and content:
                print(f"LLM 规则验证成功！已自动保存至首选规则库 (尝试次数: {attempts})")
                return timestamp, content
            else:
                # 验证失败，将刚才错误的规则弹出来，防止污染
                print(f"LLM 生成的正则未能匹配该日志，回滚并进入反思...")
                failed_attempts.append(f"{new_pattern} \n失败原因：正则匹配失败。")
                target_list.pop(0)
                self.parser.refresh_format_regex(self.log_module)

        print("达到最大重试次数，未能成功解析该行日志。")
        return None, None