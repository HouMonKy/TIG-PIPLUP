import os
import httpx
from openai import OpenAI
import base64
from pathlib import Path
import requests
import json

# 从 config.json 加载配置，改成个人配置路径
def load_llm_config():
    config_path = r'C:\Users\h50058446\.claude-code-router\config.json'
    if os.path.exists(config_path):
        with open(config_path, 'r', encoding='utf-8') as f:
            return json.load(f)
    return None

# 获取默认配置
_config = load_llm_config()

# 构建 API key 字典和 provider 字典（从所有 provider 合并）
llm_api_key_dict = {}
llm_base_url_dict = {}
provider_dict = {}
if _config and 'Providers' in _config:
    for provider in _config['Providers']:
        base_url = provider.get('api_base_url', '')
        # 移除末尾的 /chat/completions，因为 OpenAI SDK 会自动添加
        if base_url.endswith('/chat/completions'):
            base_url = base_url[:-len('/chat/completions')]
        provider_name = provider.get('name', '')
        provider_dict[provider_name] = provider
        for model in provider.get('models', []):
            llm_api_key_dict[model] = provider.get('api_key', '')
            llm_base_url_dict[model] = base_url

# 从 Router.default 获取默认配置（如 "public,MiniMax-M2.7"）
DEFAULT_MODEL = "qwen3.6-27b"  # 默认模型
DEFAULT_BASE_URL = "http://oneapi.rnd.huawei.com/v1/chat/completions"  # 默认 base_url（public provider）
DEFAULT_API_KEY = "empty"  # 默认 api_key

# 解析 Router.default 并设置为默认
if _config and 'Router' in _config:
    router_default = _config['Router'].get('default', '')
    if router_default:
        parts = router_default.split(',')
        if len(parts) >= 2:
            default_provider_name = parts[0].strip()
            default_model = parts[1].strip()
            # 查找对应的 provider
            if default_provider_name in provider_dict:
                provider = provider_dict[default_provider_name]
                base_url = provider.get('api_base_url', '')
                if base_url.endswith('/chat/completions'):
                    base_url = base_url[:-len('/chat/completions')]
                DEFAULT_BASE_URL = base_url
                DEFAULT_API_KEY = provider.get('api_key', '')
                DEFAULT_MODEL = default_model
                print(f"[LLM Config] 使用 Router.default: provider={default_provider_name}, model={DEFAULT_MODEL}, base_url={DEFAULT_BASE_URL}")


def llm_client(content:str, model_name: str=None, no_think_output=True):

    if model_name is None:
        model_name = DEFAULT_MODEL

    # 获取该模型的配置（如果存在）
    if model_name in llm_api_key_dict:
        api_key = llm_api_key_dict[model_name]
        base_url = llm_base_url_dict.get(model_name, DEFAULT_BASE_URL)
    else:
        # 使用默认配置
        api_key = DEFAULT_API_KEY
        base_url = DEFAULT_BASE_URL

    os.environ['NO_PROXY'] = 'openai.rnd.huawei.com,127.0.0.1,localhost'
    httpx_client = httpx.Client(verify=False, timeout=300)
    client = OpenAI(api_key=api_key,
                    base_url=base_url, http_client=httpx.Client(verify=False))
    response = client.chat.completions.create(
        model=model_name,
        messages=[{"role": "user", "content": content}],
        stream=False)

    output = response.choices[0].message.content

    # 使用 <think> 标签来分割思考部分和输出部分
    if no_think_output and "<think>" in output:
        # 分割并获取输出部分
        output = output.split("</think>")[-1].strip()  # 获取最后一个 <think> 后面的内容

    return output


def llm_client_stream(content:str, model_name: str=None):
    if model_name is None:
        model_name = DEFAULT_MODEL

    # 获取该模型的配置（如果存在）
    if model_name in llm_api_key_dict:
        api_key = llm_api_key_dict[model_name]
        base_url = llm_base_url_dict.get(model_name, DEFAULT_BASE_URL)
    else:
        # 使用默认配置
        api_key = DEFAULT_API_KEY
        base_url = DEFAULT_BASE_URL

    os.environ['NO_PROXY'] = 'openai.rnd.huawei.com,127.0.0.1,localhost'
    httpx_client = httpx.Client(verify=False, timeout=300)
    client = OpenAI(api_key=api_key,
                    base_url=base_url, http_client=httpx.Client(verify=False))
    response = client.chat.completions.create(
        model=model_name,
        messages=[{"role": "user", "content": content}],
        stream=True
    )
    # 逐步处理流式响应
    full_text = ''
    for chunk in response:
        # 每个 chunk 的结构类似：choices[0].delta.content
        if chunk.choices and chunk.choices[0].delta.content is not None:
            content_piece = chunk.choices[0].delta.content
            print(content_piece, end='', flush=True)  # 实时输出，不换行
            full_text += content_piece

    # 使用 <think> 标签来分割思考部分和输出部分
    if "<think>" in full_text:
        # 分割并获取输出部分
        full_text = full_text.split("</think>")[-1].strip()  # 获取最后一个 <think> 后面的内容

    return full_text

def encode_image_to_base64(image_path):
    # 读取本地图片并转换为Base64
    with open(image_path, "rb") as image_file:
        encoded_string = base64.b64encode(image_file.read()).decode('utf-8')

    # 根据文件扩展名确定 MIME 类型
    ext = Path(image_path).suffix.lower()
    mime_map = {
        '.jpg': 'image/jpeg',
        '.jpeg': 'image/jpeg',
        '.png': 'image/png',
        '.gif': 'image/gif',
        '.bmp': 'image/bmp',
        '.webp': 'image/webp',
    }
    mime_type = mime_map.get(ext, 'image/jpeg')  # 默认使用 jpeg

    return f"data:{mime_type};base64,{encoded_string}"


def vlm_client(content, image_path):

    # image_path = r'D:\NetworkMaintenance\Code2026\TechExplore\data\Picture\topo1.png'
    # 获取Base64编码
    base64_data_url = encode_image_to_base64(image_path)

    # 去掉代理和SSL认证
    os.environ['NO_PROXY'] = 'openai.rnd.huawei.com,127.0.0.1,localhost'
    httpx_client = httpx.Client(verify=False, timeout=300)

    client = OpenAI(api_key='sk-CfNhGQBGK0vtwr59168e59053e8c48A0B204BfD21f7bEbEa', base_url="https://oneapi.rnd.huawei.com/v1", http_client=httpx.Client(verify=False))

    response = client.chat.completions.create(
        model="Qwen3-VL-32B-Instruct",  # --模型标识
        messages=[{
                "role": "user",
                "content": [
                    {
                        "type": "text",
                        "text": content
                    },
                    {
                        "type": "image_url",
                        "image_url": {
                            # 使用Base64格式，注意前缀
                        # "url": f"data:image/jpeg;base64,{base64_image}"
                        "url": base64_data_url
                        }
                    }
                ]
            }],
        stream=False
    )
    # print(response.choices[0].message.content)

    return response.choices[0].message.content


def llm_client_embedding(text, model_name='Qwen3-Embedding-4B'):

    # 去掉代理和SSL认证
    os.environ['NO_PROXY'] = 'oneapi.rnd.huawei.com'
    httpx_client = httpx.Client(verify=False, timeout=300)
    client = OpenAI(api_key=llm_api_key_dict[model_name], base_url="https://oneapi.rnd.huawei.com/v1",
                    http_client=httpx.Client(verify=False))

    response = client.embeddings.create(
        input=text,
        model=model_name
    )
    return response.data[0].embedding


def llm_client_adn(content:str, model_name: str=None, no_think_output=True):
    if model_name is None:
        model_name = 'glm4.7'

    os.environ['NO_PROXY'] = 'openai.rnd.huawei.com,127.0.0.1,localhost'
    httpx_client = httpx.Client(verify=False, timeout=300)
    client = OpenAI(api_key='',
                    base_url="http://110.7.97.137:8080/v1", http_client=httpx.Client(verify=False))
    response = client.chat.completions.create(
        model=model_name,
        messages=[{"role": "user", "content": content}],
        stream=False)

    output = response.choices[0].message.content

    # 使用 <think> 标签来分割思考部分和输出部分
    if no_think_output and "<think>" in output:
        # 分割并获取输出部分
        output = output.split("</think>")[-1].strip()  # 获取最后一个 <think> 后面的内容

    return output

def call_other_model(content):
    os.environ['NO_PROXY'] = 'openai.rnd.huawei.com,127.0.0.1,localhost'
    client = OpenAI(
        api_key='sk-1234',
        base_url='http://api.openai.rnd.huawei.com/v1',
        http_client=httpx.Client(verify=False)
    )
    payload = {
        "model": 'qwen3-coder-480b-a35b-instruct',
        "messages": [{"role": "user", "content": f"{content}"}],
        "max_tokens": 10000,
        "temperature": 0.7,
    }
    response = client.chat.completions.create(**payload)

    return response

def call_other_model_re(content):

    url = "http://api.openai.rnd.huawei.com/v1/chat/completions"

    headers = {
        "Authorization": "Bearer sxxx",
        "Content-Type": "application/json"
    }

    data = {
        "model": "qwen3-coder-480b-a35b-instruct",
        "messages": [
            {"role": "user", "content": content}
        ],
        "max_tokens": 10000,
        "temperature": 0.7
    }

    r = requests.post(url, headers=headers, json=data)

    r.raise_for_status()

    return r.json()["choices"][0]["message"]["content"]


def other_llm_client(content=None):
    content = 'hello' if content is None else content
    model_name = 'deepseek-v3-0324-fc'
    os.environ['NO_PROXY'] = 'openai.rnd.huawei.com,127.0.0.1,localhost'
    httpx_client = httpx.Client(verify=False, timeout=300)
    client = OpenAI(api_key="sk-1234",
                    base_url="http://api.openai.rnd.huawei.com/v1", http_client=httpx.Client(verify=False))
    response = client.chat.completions.create(
        model=model_name,
        messages=[{"role": "user", "content": content}],
        stream=True
    )
    # 逐步处理流式响应
    full_text = ''
    for chunk in response:
        # 每个 chunk 的结构类似：choices[0].delta.content
        if chunk.choices and chunk.choices[0].delta.content is not None:
            content_piece = chunk.choices[0].delta.content
            print(content_piece, end='', flush=True)  # 实时输出，不换行
            full_text += content_piece
    print('\n')

    # 使用 <think> 标签来分割思考部分和输出部分
    if "<think>" in full_text:
        # 分割并获取输出部分
        full_text = full_text.split("</think>")[-1].strip()  # 获取最后一个 <think> 后面的内容

    return full_text


def llm_client_test(content:str, model_name: str=None, no_think_output=True):
    if model_name is None:
        model_name = 'Qwen3-Coder-30B-A3B-Instruct'
    if model_name not in llm_api_key_dict:
        return f'The specified model is not supported: {model_name}'
    os.environ['NO_PROXY'] = 'openai.rnd.huawei.com,127.0.0.1,localhost'
    httpx_client = httpx.Client(verify=False, timeout=300)
    client = OpenAI(api_key='123rtgfd',
                    base_url="https://snapengine.cida.cce.prod-szv-y.dragon.tools.huawei.com/api/v2/chat/completions", http_client=httpx.Client(verify=False))
    response = client.chat.completions.create(
        model=model_name,
        messages=[{"role": "user", "content": content}],
        stream=True
    )
    # 逐步处理流式响应
    full_text = ''
    for chunk in response:
        # 每个 chunk 的结构类似：choices[0].delta.content
        if chunk.choices and chunk.choices[0].delta.content is not None:
            content_piece = chunk.choices[0].delta.content
            print(content_piece, end='', flush=True)  # 实时输出，不换行
            full_text += content_piece
    print('\n')

    # 使用 <think> 标签来分割思考部分和输出部分
    if "<think>" in full_text and no_think_output:
        # 分割并获取输出部分
        full_text = full_text.split("</think>")[-1].strip()  # 获取最后一个 <think> 后面的内容

    return full_text


def llm_client_ca(content:str, model_name: str=None):
    if model_name is None:
        model_name = 'codemate/MiniMax-M2.5'
    os.environ['NO_PROXY'] = '127.0.0.1'
    httpx_client = httpx.Client(verify=False, timeout=300)
    client = OpenAI(api_key="sk-anything-will-be-ok",
                    base_url="http://127.0.0.1:31943/v1/", http_client=httpx.Client(verify=False))
    response = client.chat.completions.create(
        model=model_name,
        messages=[{"role": "user", "content": content}],
        stream=True
    )
    # 逐步处理流式响应
    full_text = ''
    for chunk in response:
        # 每个 chunk 的结构类似：choices[0].delta.content
        if chunk.choices and chunk.choices[0].delta.content is not None:
            content_piece = chunk.choices[0].delta.content
            print(content_piece, end='', flush=True)  # 实时输出，不换行
            full_text += content_piece
    print(f'token 生成长度{len(full_text)} \n')

    # 使用 <think> 标签来分割思考部分和输出部分
    if "<think>" in full_text:
        # 分割并获取输出部分
        full_text = full_text.split("</think>")[-1].strip()  # 获取最后一个 <think> 后面的内容

    return full_text


if __name__ == '__main__':
    content = '你好！'
    respone = llm_client_stream(content)
    print(respone)