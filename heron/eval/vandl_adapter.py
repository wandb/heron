import base64
import requests
import os
import logging
import importlib
import torch
import ast
import hydra
from PIL import Image
from tenacity import retry, stop_after_attempt, wait_fixed
from config_singleton import WandbConfigSingleton

from heron.models.video_blip import VideoBlipForConditionalGeneration, VideoBlipProcessor
from heron.models.git_llm.git_japanese_stablelm_alpha import GitJapaneseStableLMAlphaForCausalLM
from transformers import LlamaTokenizer, AutoTokenizer, AutoProcessor

from transformers import AutoTokenizer, AutoModelForCausalLM, StoppingCriteria
from transformers import AutoTokenizer, AutoModelForVision2Seq, AutoImageProcessor
from datasets import load_dataset

import requests
from PIL import Image
import wandb

HERON_TYPE1_LIST = [
    "turing-motors/heron-chat-git-ja-stablelm-base-7b-v1",
    "turing-motors/heron-chat-blip-ja-stablelm-base-7b-v1-llava-620k",
    "turing-motors/heron-chat-blip-ja-stablelm-base-7b-v1",
    "turing-motors/heron-chat-blip-ja-stablelm-base-7b-v0",
]
JAPANESE_STABLEVLM_LIST = [
    'stabilityai/japanese-stable-vlm',
]
QWEN_VL_LIST = [
    'Qwen/Qwen-VL-Chat',
]
LLAVA_LIST = [
    'liuhaotian/llava-v1.6-vicuna-7b',
    'liuhaotian/llava-v1.6-vicuna-13b',
    'liuhaotian/llava-v1.6-mistral-7b',
    'liuhaotian/llava-v1.6-34b',
    'liuhaotian/llava-v1.5-7b',
    'liuhaotian/llava-v1.5-13b',
    'liuhaotian/llava-v1.5-7b-lora',
    'liuhaotian/llava-v1.5-13b-lora',
]
EvoVLM = [
    'SakanaAI/EvoVLM-JP-v1-7B',
]
LLaVACALM2 = [
    'cyberagent/llava-calm2-siglip',
]

def load_processor(cfg):
    if cfg.tokenizer is None:
        processor = hydra.utils.instantiate(cfg.processor, _recursive_=False)
    else:
        tokenizer_args = {}
        if cfg.tokenizer.args is not None:
            tokenizer_args = {k: v for k, v in cfg.tokenizer.args.items() if v is not None}
        if tokenizer_args.get("additional_special_tokens"):
            additional_special_tokens = ast.literal_eval(tokenizer_args['additional_special_tokens'])
            del tokenizer_args['additional_special_tokens']
            tokenizer = hydra.utils.call(cfg.tokenizer, **tokenizer_args, _recursive_=False)
            tokenizer.add_special_tokens({"additional_special_tokens": additional_special_tokens})
        else:
            tokenizer = hydra.utils.call(cfg.tokenizer, **tokenizer_args, _recursive_=False)
        processor = hydra.utils.call(cfg.processor, _recursive_=False)
        processor.tokenizer = tokenizer
    return processor

# APIキーの取得
api_key = os.getenv('OPENAI_API_KEY')

# 画像をBase64にエンコードする関数
def encode_image(image_path):
    with open(image_path, "rb") as image_file:
        return base64.b64encode(image_file.read()).decode('utf-8')

# for OpenAI API
class OpenAIResponseGenerator:
    def __init__(self, api_key, model_name="gpt-4-turbo-2024-04-09", max_tokens=4000, temperature=0.0):
        self.api_key = api_key
        self.model_name = model_name
        self.max_tokens = max_tokens
        self.temperature = temperature

    @retry(stop=stop_after_attempt(5), wait=wait_fixed(30))
    def generate_response(self, prompt, image_path):
        """
        OpenAI APIにリクエストを送信するメソッド。
        リトライ処理を追加し、失敗した場合は例外を発生させる。
        """
        base64_image = encode_image(image_path)

        headers = {
            "Content-Type": "application/json",
            "Authorization": f"Bearer {self.api_key}"
        }
        payload = {
            "model": self.model_name,
            "messages": [
                {
                    "role": "user",
                    "content": [
                        {
                            "type": "text",
                            "text": prompt
                        },
                        {
                            "type": "image_url",
                            "image_url": {
                                "url": f"data:image/jpeg;base64,{base64_image}"
                            }
                        }
                    ]
                }
            ],
            "max_tokens": self.max_tokens,
            "temperature": self.temperature
        }
        response = requests.post("https://api.openai.com/v1/chat/completions", headers=headers, json=payload)
        response.raise_for_status()
        return response.json()["choices"][0]["message"]["content"]

class HeronType1ResponseGenerator:
    def __init__(self, model, processor, device):
        self.model = model
        self.processor = processor
        self.device = device
        logging.basicConfig(level=logging.INFO)
        self.cfg = WandbConfigSingleton.get_instance().config

    def generate_response(self, question, image_path):
        image = Image.open(image_path)
        text = f"##human: {question}\n##gpt: "
        print(text)  # for debug
        inputs = self.processor(text=text, images=image, return_tensors="pt", truncation=True)
        inputs = {k: v.to(self.device) for k, v in inputs.items()}
        inputs["pixel_values"] = inputs["pixel_values"].to(self.device).half()

        logging.info(f"Input text: {text}")
        logging.info(f"Input shapes: { {k: v.shape for k, v in inputs.items()} }")
        logging.info(f"Using device: {self.device}")

        eos_token_id_list = [
            self.processor.tokenizer.pad_token_id,
            self.processor.tokenizer.eos_token_id,
            int(self.processor.tokenizer.convert_tokens_to_ids("\n")),
        ]
        eos_token_id_list += ast.literal_eval(self.cfg.generation.args.eos_token_id_list)

        try:
            with torch.no_grad():
                out = self.model.generate(
                    **inputs,
                    max_length=self.cfg.generation.args.max_length,
                    do_sample=self.cfg.generation.args.do_sample,
                    temperature=self.cfg.generation.args.temperature,
                    eos_token_id=eos_token_id_list,
                    no_repeat_ngram_size=self.cfg.generation.args.no_repeat_ngram_size,
                )
            return self.processor.tokenizer.batch_decode(out, skip_special_tokens=True)[0].split('##gpt:')[1]
        except Exception as e:
            logging.error(f"Error during model generation: {e}")
            logging.info(f"Inputs at error: {inputs}")
            raise e

# for japanese-stable-vlm
# helper function to format input prompts
TASK2INSTRUCTION = {
    "caption": "画像を詳細に述べてください。",
    "tag": "与えられた単語を使って、画像を詳細に述べてください。",
    "vqa": "与えられた画像を下に、質問に答えてください。",
}

def build_prompt(task="vqa", input=None, sep="\n\n### "):
    assert (
        task in TASK2INSTRUCTION
    ), f"Please choose from {list(TASK2INSTRUCTION.keys())}"
    if task in ["tag", "vqa"]:
        assert input is not None, "Please fill in `input`!"
        if task == "tag" and isinstance(input, list):
            input = "、".join(input)
    else:
        assert input is None, f"`{task}` mode doesn't support to input questions"
    sys_msg = "以下は、タスクを説明する指示と、文脈のある入力の組み合わせです。要求を適切に満たす応答を書きなさい。"
    p = sys_msg
    roles = ["指示", "応答"]
    instruction = TASK2INSTRUCTION[task]
    msgs = [": \n" + instruction, ": \n"]
    if input:
        roles.insert(1, "入力")
        msgs.insert(1, ": \n" + input)
    for role, msg in zip(roles, msgs):
        p += sep + role + msg
    return p

class JapanseseStableVLMResponseGenerator:
    def __init__(self, model, processor, tokenizer, device):
        self.model = model
        self.processor = processor
        self.tokenizer = tokenizer
        self.device = device
        self.cfg = WandbConfigSingleton.get_instance().config

    @torch.inference_mode()
    def generate_response(self, question, image_path):
        image = Image.open(image_path)
        prompt = build_prompt(task="vqa", input=question)
        
        inputs = self.processor(images=[image], return_tensors="pt")
        text_encoding = self.tokenizer(prompt, add_special_tokens=False, return_tensors="pt")
        inputs.update(text_encoding)
        
        generation_kwargs = {
            "do_sample": False,
            "max_new_tokens": self.cfg.generation.args.max_length,
            "temperature":self.cfg.generation.args.temperature,
            "min_length": 1,
            "top_p": 0,
            "no_repeat_ngram_size": self.cfg.generation.args.no_repeat_ngram_size,
        }
        
        try:
            outputs = self.model.generate(
                **inputs.to(self.device, dtype=self.model.dtype), 
                **generation_kwargs
            )
            generated = [
                txt.strip() for txt in self.tokenizer.batch_decode(outputs, skip_special_tokens=True)
            ]
            return generated[0]
        
        except Exception as e:
            logging.error(f"Error during model generation: {e}")
            raise e
        finally:
            del inputs
            del outputs
            torch.cuda.empty_cache()


# for Qwen/Qwen-VL-Chat
class QwenVLChatResponseGenerator:
    def __init__(self, model, tokenizer, device):
        self.model = model
        self.tokenizer = tokenizer
        self.device = device
        self.cfg = WandbConfigSingleton.get_instance().config

    @torch.inference_mode()
    def generate_response(self, question, image_path):
        query = self.tokenizer.from_list_format([
            {'image': image_path},
            {'text': question},
        ])
        response, history = self.model.chat(self.tokenizer, query=query, history=None)
        return response

# for Gemini
import os
import google.generativeai as genai
from PIL import Image
from config_singleton import WandbConfigSingleton

class GeminiResponseGenerator:
    def __init__(self, api_key):
        self.api_key = api_key
        self.cfg = WandbConfigSingleton.get_instance().config
        
        self.model_name = self.cfg.model.pretrained_model_name_or_path
        
        self.generation_config = {
            "temperature": self.cfg.generation.args.temperature,
            "max_output_tokens": self.cfg.generation.args.max_length,
        }
            
        genai.configure(api_key=self.api_key)
        self.model = genai.GenerativeModel(self.model_name, generation_config=self.generation_config)

    def generate_response(self, question, image_path):
        image = Image.open(image_path)
        message = [question, image]
        response = self.model.generate_content(message)

        if hasattr(response._result, 'candidates') and response._result.candidates:
            candidate = response._result.candidates[0]
            answer = "".join(part.text for part in candidate.content.parts) if candidate.content.parts else "empty response"
        else:
            answer = "Blocked by the safety filter."

        return answer


# for LLaVA
import os
import torch
import re
from PIL import Image
from config_singleton import WandbConfigSingleton

# for LLaVA
class LLaVAResponseGenerator:
    def __init__(self, model_path, device):
        self.cfg = WandbConfigSingleton.get_instance().config
        
        self.model_path = model_path
        self.model_name = get_model_name_from_path(model_path)
        
        from llava.constants import (
            IMAGE_TOKEN_INDEX,
            DEFAULT_IMAGE_TOKEN,
            DEFAULT_IM_START_TOKEN,
            DEFAULT_IM_END_TOKEN,
            IMAGE_PLACEHOLDER,
        )
        from llava.conversation import conv_templates, SeparatorStyle
        from llava.model.builder import load_pretrained_model
        from llava.mm_utils import (
            process_images,
            tokenizer_image_token,
            get_model_name_from_path,
        )
        
        self.tokenizer, self.model, self.image_processor, self.context_len = load_pretrained_model(
            self.model_path, None, self.model_name
        )
        
        self.device = device
        self.model.eval()
        self.model.to(self.device)

    def generate_response(self, question, image_path):
        image = Image.open(image_path)
        
        # prepare inputs
        qs = question
        image_token_se = DEFAULT_IM_START_TOKEN + DEFAULT_IMAGE_TOKEN + DEFAULT_IM_END_TOKEN
        if IMAGE_PLACEHOLDER in qs:
            if self.model.config.mm_use_im_start_end:
                qs = re.sub(IMAGE_PLACEHOLDER, image_token_se, qs)
            else:
                qs = re.sub(IMAGE_PLACEHOLDER, DEFAULT_IMAGE_TOKEN, qs)
        else:
            if self.model.config.mm_use_im_start_end:
                qs = image_token_se + "\n" + qs
            else:
                qs = DEFAULT_IMAGE_TOKEN + "\n" + qs
        
        conv = conv_templates["vicuna_v1"].copy()
        conv.append_message(conv.roles[0], qs)
        conv.append_message(conv.roles[1], None)
        prompt = conv.get_prompt()
        
        images = [image]
        
        image_sizes = [x.size for x in images]
        images_tensor = process_images(
            images,
            self.image_processor,
            self.model.config
        ).to(self.model.device, dtype=torch.float16)
        
        input_ids = (
            tokenizer_image_token(prompt, self.tokenizer, IMAGE_TOKEN_INDEX, return_tensors="pt")
            .unsqueeze(0)
            .cuda()
        )
        
        with torch.inference_mode():
            output_ids = self.model.generate(
                input_ids,
                images=images_tensor,
                image_sizes=image_sizes,
                max_new_tokens=self.cfg.generation.args.max_length,
                do_sample=self.cfg.generation.args.do_sample,
                temperature=self.cfg.generation.args.temperature,
                use_cache=True,
                no_repeat_ngram_size=self.cfg.generation.args.no_repeat_ngram_size,
            )
        res = self.tokenizer.batch_decode(output_ids, skip_special_tokens=True)
        return res[0]


# for Claude-3
import os
import io
import base64
from PIL import Image
from anthropic import Anthropic
from config_singleton import WandbConfigSingleton

class ClaudeResponseGenerator:
    def __init__(self, api_key):
        self.api_key = api_key
        self.cfg = WandbConfigSingleton.get_instance().config
        
        self.model_name = self.cfg.model.pretrained_model_name_or_path
        self.client = Anthropic(api_key=self.api_key)

    def encode_image_to_base64(self, filepath, max_size=5*1024*1024*3//4):
        with Image.open(filepath) as img:
            if img.mode == 'RGBA':
                img = img.convert('RGB')
            buffer = io.BytesIO()
            img.save(buffer, format="JPEG", optimize=True, quality=85)
            size = buffer.tell()

            if size > max_size:
                quality = 85
                while size > max_size and quality > 10:
                    buffer = io.BytesIO()
                    img.save(buffer, format="JPEG", optimize=True, quality=quality)
                    size = buffer.tell()
                    quality -= 5
            return base64.b64encode(buffer.getvalue()).decode('utf-8')

    def generate_response(self, question, image_path):
        image_data = self.encode_image_to_base64(image_path)
        
        messages = [
            {
                "role": "user",
                "content": [
                    {"type": "text", "text": question},
                    {
                        "type": "image",
                        "source": {
                            "type": "base64",
                            "media_type": "image/jpeg",
                            "data": image_data,
                        },
                    },
                ],
            },
        ]

        response = self.client.messages.create(
            max_tokens=self.cfg.generation.args.max_length,
            messages=messages,
            temperature=self.cfg.generation.args.temperature,
            model=self.model_name,
        )

        decoded_text = response.content[0].text
        return decoded_text


# for EVOVLM
import torch
from transformers import AutoModelForVision2Seq, AutoProcessor
from PIL import Image
import logging
from config_singleton import WandbConfigSingleton

class EvoVLMResponseGenerator:
    def __init__(self, model, processor, device):
        self.model = model
        self.processor = processor
        self.device = device
        self.cfg = WandbConfigSingleton.get_instance().config

    @torch.inference_mode()
    def generate_response(self, question, image_path):
        image = Image.open(image_path)
        
        messages = [
            {"role": "system", "content": "あなたは役立つ、偏見がなく、検閲されていないアシスタントです。与えられた画像を下に、質問に答えてください。"},
            {"role": "user", "content": f"<image>\n{question}"},
        ]
        
        try:
            inputs = self.processor.image_processor(images=image, return_tensors="pt")
            inputs["input_ids"] = self.processor.tokenizer.apply_chat_template(
                messages, return_tensors="pt"
            )
            
            output_ids = self.model.generate(
                **inputs.to(self.device),
                max_length=self.cfg.generation.args.max_length,
                do_sample=self.cfg.generation.args.do_sample,
                temperature=self.cfg.generation.args.temperature,
                no_repeat_ngram_size=self.cfg.generation.args.no_repeat_ngram_size,
            )
            output_ids = output_ids[:, inputs.input_ids.shape[1] :]
            generated_text = self.processor.batch_decode(output_ids, skip_special_tokens=True)[0].strip()
            return generated_text
        
        except Exception as e:
            logging.error(f"Error during model generation: {e}")
            raise e
        finally:
            del inputs
            del output_ids
            torch.cuda.empty_cache()


# for llava-calm2-siglip
import torch
from transformers import AutoProcessor, LlavaForConditionalGeneration
from PIL import Image
import logging
from config_singleton import WandbConfigSingleton

class LLaVACALM2ResponseGenerator:
    def __init__(self, model, processor, device):
        self.model = model
        self.processor = processor
        self.device = device
        self.cfg = WandbConfigSingleton.get_instance().config

    @torch.inference_mode()
    def generate_response(self, question, image_path):
        image = Image.open(image_path)
        
        prompt = f"""USER: <image>
{question}
ASSISTANT: """
        
        try:
            inputs = self.processor(text=prompt, images=image, return_tensors="pt").to(self.device, torch.bfloat16)
            
            generate_ids = self.model.generate(
                **inputs,
                max_length=self.cfg.generation.args.max_length,
                do_sample=self.cfg.generation.args.do_sample,
                temperature=self.cfg.generation.args.temperature,
                no_repeat_ngram_size=self.cfg.generation.args.no_repeat_ngram_size,
            )
            
            output = self.processor.tokenizer.decode(generate_ids[0][:-1], clean_up_tokenization_spaces=False)
            response = output.split("ASSISTANT: ")[1]
            return response
        
        except Exception as e:
            logging.error(f"Error during model generation: {e}")
            raise e
        finally:
            del inputs
            del generate_ids
            torch.cuda.empty_cache()


# for microsoft/Phi-3-vision-128k-instruct
import torch
from transformers import AutoModelForCausalLM, AutoProcessor
from PIL import Image
import logging
from config_singleton import WandbConfigSingleton

class Phi3Vision128KInstructResponseGenerator:
    def __init__(self, model, processor, device):
        self.model = model
        self.processor = processor
        self.device = device
        self.cfg = WandbConfigSingleton.get_instance().config

    @torch.inference_mode()
    def generate_response(self, question, image_path):
        image = Image.open(image_path)
        
        messages = [
            {"role": "user", "content": f"<|image_1|>\n{question}"},
        ]
        
        try:
            prompt = self.processor.tokenizer.apply_chat_template(messages, tokenize=False, add_generation_prompt=True)
            inputs = self.processor(prompt, [image], return_tensors="pt").to(self.device)
            
            generation_args = {
                "max_new_tokens": self.cfg.generation.args.max_length,
                "temperature": self.cfg.generation.args.temperature,
                "do_sample": self.cfg.generation.args.do_sample,
            }
            
            generate_ids = self.model.generate(
                **inputs,
                eos_token_id=self.processor.tokenizer.eos_token_id, 
                no_repeat_ngram_size=self.cfg.generation.args.no_repeat_ngram_size,
                **generation_args
            )
            
            generate_ids = generate_ids[:, inputs['input_ids'].shape[1]:]
            response = self.processor.batch_decode(generate_ids, skip_special_tokens=True, clean_up_tokenization_spaces=False)[0] 

            return response
        
        except Exception as e:
            logging.error(f"Error during model generation: {e}")
            raise e
        finally:
            del inputs
            del generate_ids
            torch.cuda.empty_cache()


# for automatic adapter generation
import os
from huggingface_hub import hf_hub_download
import ast
import openai
import anthropic
from openai import OpenAI

def generate_automatic_adapter(model_id, api_type):
    instance = WandbConfigSingleton.get_instance()
    run = instance.run
    cfg = instance.config
    
    # Download the model's README.md from Hugging Face
    readme_path = hf_hub_download(repo_id=model_id, filename="README.md")

    with open(readme_path, "r") as f:
        readme_content = f.read()

    # Read the content of vandl_adapter.py and run_eval.py
    with open("vandl_adapter.py", "r") as f:
        vandl_adapter_content = f.read()

    with open("run_eval.py", "r") as f:
        run_eval_content = f.read()

    # Construct the prompt for GPT-4 or Claude-3-Opus
    prompt = f"""
    Given the following README.md content for the model {model_id}:

    {readme_content}

    And the content of vandl_adapter.py:

    {vandl_adapter_content}

    And the content of run_eval.py:

    {run_eval_content}

    Please generate a Python code snippet for an adapter class that can be used to run the model for the Vision and Language Leaderboard benchmark. The adapter should follow the structure of the existing adapters in vandl_adapter.py and should be compatible with the run_eval.py script. Note that ResponseGenerator can take only self as only one arg. So load model, processor or tokenizer in the class. Please ensure that the output begins with the identifier '# GENERATED_ADAPTER_CODE' on a new line, followed by the generated Python code. Do not include any additional content such as explanations or descriptions.
    """

    # Call GPT-4 or Claude-3-Opus API with the prompt
    generated_adapter = call_gpt4_or_claude_api(prompt, api_type)

    # Split the generated code using the identifier and take the second part (the actual code)
    generated_adapter_code = generated_adapter.split("# GENERATED_ADAPTER_CODE\n")[1]

    # Write the generated adapter to a new Python file
    module_name = f"adapter_{model_id.replace('/', '_').replace('.', '_').replace('-', '_')}"
    with open(f"{module_name}.py", "w") as f:
        f.write(generated_adapter_code)

    # Display the generated code before asking for user confirmation
    print("Generated code:")
    print(generated_adapter_code)

    # Ask the user for confirmation before executing the generated code
    if cfg.model.skip_approval==False:
        approval = input("Are you sure you want to execute the generated code? (y/n): ")
        if approval.lower() != 'y':
            print("Execution of the code has been denied by the user.")
            sys.exit("Execution of the code has been stopped by the user.")

    artifact = wandb.Artifact('generated_adapter', type='adapter')
    artifact.add_file(f"{module_name}.py")
    run.log_artifact(artifact)

    # Import the generated adapter dynamically
    adapter_module = importlib.import_module(module_name)
    
    # Find the adapter class name in the generated code
    class_name_match = re.search(r'\b(\w*ResponseGenerator)\b', generated_adapter_code)

    if class_name_match.group(1):
        class_name = class_name_match.group(1)
    else:
        raise ValueError(f"Could not find the adapter class name in the generated code for model {model_id}. Please make sure the class name ends with 'ResponseGenerator'.")

    adapter_class = getattr(adapter_module, class_name)
    cfg.model.class_name = class_name

    return adapter_class

# for automatic adapter generation
def call_gpt4_or_claude_api(prompt, api_type):
    if api_type == 'gpt-4':
        if os.getenv("OPENAI_API_KEY"):
            openai.api_key = os.getenv("OPENAI_API_KEY")
            client = OpenAI()
            response = client.chat.completions.create(
                model="gpt-4",
                messages=[
                    {"role": "system", "content": "You are a helpful assistant that generates code based on given requirements."},
                    {"role": "user", "content": prompt}
                ],
                max_tokens=4096,
                n=1,
                stop=None,
                temperature=0.7,
            )
            return response.choices[0].message['content']
        else:
            raise ValueError("OpenAI API key is not available.")

    elif api_type == 'claude-3-opus':
        if os.getenv("ANTHROPIC_API_KEY"):
            client = anthropic.Client()
            response = client.messages.create(
                messages=[
                    {
                        "role": "user",
                        "content": prompt
                    }
                ],
                model="claude-3-opus-20240229",
                max_tokens=4096,
                temperature=0.7,
            )
            return response.content[0].text.strip()
        else:
            raise ValueError("Anthropic API key is not available.")

    else:
        raise ValueError(f"Unsupported API type: {api_type}")
    

# Let's start preparing generator
def get_adapter():
    instance = WandbConfigSingleton.get_instance()
    cfg = instance.config

    if cfg.model.automatic_adapter_generation:
        model_id = cfg.model.pretrained_model_name_or_path
        module_name = f"adapter_{model_id.replace('/', '_').replace('.', '_').replace('-', '_')}"
        class_name = cfg.model.class_name
        
        try:
            adapter_module = importlib.import_module(module_name)
            adapter_class = getattr(adapter_module, class_name)
            device_id = 0
            device = f"cuda:{device_id}"
            generator = adapter_class(device)
            return generator
        except (ImportError, AttributeError):
            raise ImportError(f"Could not import the generated adapter for model {model_id}. Please check the logs for any errors during adapter generation.")

    if cfg.api:
        if cfg.api=="openai":
            generator = OpenAIResponseGenerator(
                api_key=os.getenv('OPENAI_API_KEY'),
                model_name=cfg.model.pretrained_model_name_or_path,
                max_tokens=cfg.generation.args.max_length,
                temperature=cfg.generation.args.temperature,
            )
            instance = WandbConfigSingleton.get_instance()
            instance.store['generator'] = generator
            return generator

        elif cfg.api=='gemini':
            api_key=os.environ["GEMINI_API_KEY"]
            generator = GeminiResponseGenerator(api_key=api_key)
            return generator

        elif cfg.api=='anthropic':
            api_key=os.environ["ANTHROPIC_API_KEY"]
            generator = ClaudeResponseGenerator(api_key=api_key)
            return generator
        
    elif cfg.model.pretrained_model_name_or_path in HERON_TYPE1_LIST:
        device_id = 0
        device = f"cuda:{device_id}"

        # Model settings
        if cfg.torch_dtype == "bf16":
            torch_dtype: torch.dtype = torch.bfloat16
        elif cfg.torch_dtype == "fp16":
            torch_dtype = torch.float16
        elif cfg.torch_dtype == "fp32":
            torch_dtype = torch.float32
        else:
            raise ValueError("torch_dtype must be bf16 or fp16. Other types are not supported.")
        model = hydra.utils.call(cfg.model, torch_dtype=torch_dtype, _recursive_=False)
        model = model.half()
        model.eval()
        model.to(device)
        print("Model loaded")

        # Processor settings
        processor = load_processor(cfg)
        print("Processor loaded")
        generator = HeronType1ResponseGenerator(model, processor, device)

        return generator

    elif cfg.model.pretrained_model_name_or_path in JAPANESE_STABLEVLM_LIST:
        device_id = 0
        device = f"cuda:{device_id}"

        max_length = cfg.generation.args.max_length
        model_path = cfg.model.pretrained_model_name_or_path
        model_name = model_path

        load_in = cfg.torch_dtype # @param ["fp32", "fp16", "int8"]
        # @markdown If you use Colab free plan, please set `load_in` to `int8`. But, please remember that `int8` degrades the performance. In general, `fp32` is better than `fp16` and `fp16` is better than `int8`.

        model_kwargs = {"trust_remote_code": True, "low_cpu_mem_usage": True}
        if load_in == "fp16":
            model_kwargs["variant"] = "fp16"
            model_kwargs["torch_dtype"] = torch.float16
        elif load_in == "int8":
            model_kwargs["variant"] = "fp16"
            model_kwargs["load_in_8bit"] = True
            model_kwargs["max_memory"] = f'{int(torch.cuda.mem_get_info()[0]/1024**3)-2}GB'

        model = AutoModelForVision2Seq.from_pretrained(model_path, **model_kwargs)
        processor = AutoImageProcessor.from_pretrained(model_path)
        tokenizer = AutoTokenizer.from_pretrained(model_path)

        model.eval()
        model.to(device)
        print('Load model')

        generator = JapanseseStableVLMResponseGenerator(model, processor, tokenizer, device)

        return generator

    elif cfg.model.pretrained_model_name_or_path in QWEN_VL_LIST:
        device_id = 0
        device = f"cuda:{device_id}"

        max_length = cfg.generation.args.max_length
        model_path = cfg.model.pretrained_model_name_or_path
        model_name = model_path

        #load_in = cfg.torch_dtype # @param ["fp32", "fp16", "int8"]
        model_kwargs = {"trust_remote_code": True, "low_cpu_mem_usage": True}

        model = AutoModelForCausalLM.from_pretrained(model_path, device_map="cuda", trust_remote_code=True)
        tokenizer = AutoTokenizer.from_pretrained(model_path, trust_remote_code=True)

        generator = QwenVLChatResponseGenerator(model, tokenizer, device)

        return generator

    elif cfg.model.pretrained_model_name_or_path in LLAVA_LIST:
        device_id = 0
        device = f"cuda:{device_id}"
        generator = LLaVAResponseGenerator(cfg.model.pretrained_model_name_or_path, device)

        return generator

    elif cfg.model.pretrained_model_name_or_path in EvoVLM:
        device_id = 0
        device = f"cuda:{device_id}"

        model_id = cfg.model.pretrained_model_name_or_path
        model = AutoModelForVision2Seq.from_pretrained(model_id, torch_dtype=torch.float16)
        processor = AutoProcessor.from_pretrained(model_id)
        model.to(device)

        generator = EvoVLMResponseGenerator(model, processor, device)

        return generator

    elif cfg.model.pretrained_model_name_or_path in LLaVACALM2:
        device_id = 0
        device = f"cuda:{device_id}"

        model = LlavaForConditionalGeneration.from_pretrained(
            cfg.model.pretrained_model_name_or_path,
            torch_dtype=torch.bfloat16,
        ).to(device)

        processor = AutoProcessor.from_pretrained(cfg.model.pretrained_model_name_or_path)

        generator = LLaVACALM2ResponseGenerator(model, processor, device)

        return generator

    elif cfg.model.pretrained_model_name_or_path == 'microsoft/Phi-3-vision-128k-instruct':
        device_id = 0
        device = f"cuda:{device_id}"

        model = AutoModelForCausalLM.from_pretrained(
            cfg.model.pretrained_model_name_or_path, 
            device_map="cuda", 
            trust_remote_code=True, 
            torch_dtype="auto", 
            _attn_implementation='flash_attention_2',
        )
        processor = AutoProcessor.from_pretrained(cfg.model.pretrained_model_name_or_path, trust_remote_code=True)

        generator = Phi3Vision128KInstructResponseGenerator(model, processor, device)

        return generator