base_prompt1 = """
/no_think
你是一个日志分析专家。需要根据输入的日志原文内容,写出正则表达式模板,以分离出日志的Time和content部分。

## 重要：输出格式规范
1. 使用 <> 包裹字段名，如 `<Time>`, `<Level>`, `<Content>`, `<Year>`, `<Month>`, `<Hour>` 等
2. **方括号转义**：如果日志中包含字面 `[` 或 `]`，在输出时必须写成 `\\[` 和 `\\]`
   - 正确：`\\[<Level>\\]` 表示匹配字面的 `[INFO]`
   - 错误：`[<Level>]` 会导致正则解析错误
3. JSON 字符串中的反斜杠必须双写：`\\[` 而不是 `\[`

## 示例1：
### 输入日志
[2025-08-01 13:00:00.179][1500000670000][INFO]ctrler(0x7f8a44bba0d0:3) client conn...
### 输出
{
"new_pattern": "\\[<Time>\\]\\[<Segment1>\\]\\[<Level>\\]<Content>"
}
### 说明
方括号 `[` 和 `]` 在正则中有特殊含义，所以必须转义为 `\\[` 和 `\\]`

## 示例2：
### 输入日志
2026-06-10 10:30:15.123 [INFO] System started successfully
### 输出
{
"new_pattern": "<Time> \\[<Level>\\] <Content>"
}
### 说明
注意方括号前的空格，方括号本身转义为 `\\[` 和 `\\]`

## 示例3：
### 输入日志
Jul 22 09:31:02 allinonefm0 su[172609]: (to galaxmanager) root on none
### 输出
{
"new_pattern": "<Month> <Date> <Hour> <Segment1> <Segment2>:<Content>"
}
### 说明
时间被空格拆分，需要用 Month、Date、Hour 等字段名

## 示例4：
### 输入日志
2025 Jul 22 09:31:02.184 allinonefm0 su[172609]: (to galaxmanager) root on none
### 输出
{
"new_pattern": "<Year> <Month> <Date> <Hour>\\.<TZ> <Segment1> <Segment2>:<Content>"
}
### 说明
小数点 `.` 在正则中有特殊含义，需要转义为 `\\.`

## Level说明
Level一般为：INFO、WARN、ERR（或ERROR）等，表示该行日志的重要/异常程度。日志时间戳和正文content是必有的，Level不一定。

## 当前输入要解析的日志原文：

"""


base_prompt2 = """

## 输出要求
1. 只输出 JSON 格式的答案，不要有任何解释文字
2. JSON 中的字符串必须正确转义反斜杠
3. 正则元字符 `[ ] ( ) . * + ? ^ $ { } | \` 必须正确转义

"""


def build_prompt(question, reflection_context):
    return base_prompt1 + question + reflection_context + base_prompt2