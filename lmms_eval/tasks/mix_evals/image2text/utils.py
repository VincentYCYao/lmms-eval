import ast
import datetime
import json
import os
import random
import re
import sys
import time
from pathlib import Path

import openai
import yaml
from loguru import logger as eval_logger
from PIL import Image

import lmms_eval.tasks._task_utils.file_utils as file_utils
from lmms_eval.filters import Filter

with open(Path(__file__).parent / "_default_template_yaml", "r") as f:
    raw_data = f.readlines()
    safe_data = []
    for i, line in enumerate(raw_data):
        # remove function definition since yaml load cannot handle it
        if "!function" not in line:
            safe_data.append(line)

    config = yaml.safe_load("".join(safe_data))

NUM_SECONDS_TO_SLEEP = 5
API_TYPE = os.getenv("API_TYPE", "openai")
MODEL_VERSION = "gpt-3.5-turbo-0125"
MAX_NEW_TOKENS = 999

if API_TYPE == "openai":
    client = openai.OpenAI()
elif API_TYPE == "azure":
    if "AZURE_ENDPOINT" in os.environ:
        API_URL = os.getenv("AZURE_ENDPOINT", "https://api.cognitive.microsoft.com/sts/v1.0/issueToken")
    else:
        API_URL = os.getenv("AZURE_OPENAI_ENDPOINT", "https://api.cognitive.microsoft.com/sts/v1.0/issueToken")
    if "AZURE_OPENAI_API_KEY" in os.environ:
        API_KEY = os.getenv("AZURE_OPENAI_API_KEY", "YOUR_API_KEY")
    else:
        API_KEY = os.getenv("AZURE_API_KEY", "YOUR_API_KEY")
    client = openai.AzureOpenAI(api_key=API_KEY, azure_endpoint=API_URL)


image2text_gpt_judge_for_closeended_freeform = lambda prompt, gold_ans, response: [
    {"role": "system", "content": f"In this task, I want you to act as a judge."},
    {
        "role": "user",
        "content": f"""You will be provided with a question, its golden answer(s), and the model's answer, while the context of the question, which is one or more images, is not given here. Your task is to judge how correct the model's answer is based on the golden answer(s), without seeing the input images of the question, and then give a correctness score. The correctness score should be one of the below numbers: 0.0 (totally wrong), 0.1, 0.2, 0.3, 0.4, 0.5, 0.6, 0.7, 0.8, 0.9, or 1.0 (totally right). Your should first briefly give your reasoning process regarding how the model's answer conforms to or contradicts the golden answer(s), and then give the correctness score. The correctness score must strictly follow this format: \"[[score]]\", e.g., \"The correctness score: [[0.5]]\". Below are some examples. 

Example 1: 
Question: what is this advertising?
Golden Answer(s): <answer 1> garden annual; <answer 2> seeds; <answer 3> seeds; <answer 4> seeds; <answer 5> seeds; <answer 6> seeds; <answer 7> seeds; <answer 8> seeds; <answer 9> seeds; <answer 10> cole's garden annual
Model's Answer: Seed
Your Judgment: The golden answers consistently mention "seeds" suggesting an advertisement for a seed catalog. The model's answer, "Seed", aligns exactly with this description. The Correctness Score: [[1.0]]

Example 2: 
Question: Who is making a face?
Golden Answer: <answer 1> child
Model's Answer: A man.
Your Judgment: The golden answer specifies a "child" making a face, but the model answered "A man", which is incorrect as it refers to a different age group. The Correctness Score: [[0.0]]

Example 3: 
Question: what road is to the right?
Golden Answer: <answer 1> troublesome valley rd; <answer 2> troublesome valley rd.; <answer 3> troublesome valley; <answer 4> troublesome valley road; <answer 5> valley road; <answer 6> troublesome valley; <answer 7> troublesome valley road; <answer 8> troublesome valley ; <answer 9> troublesome valley rd; <answer 10> troublesome valley rd.
Model's Answer: troublesome road
Your Judgment: The golden answers all specify the name of the road as "troublesome valley rd" or variations of this phrase with consistent reference to "troublesome valley." The model's answer, "troublesome road," captures the "troublesome" aspect but omits the critical "valley" part of the name, which is crucial for full accuracy. Thus, the model's answer partially matches the golden answer but lacks complete specificity. The Correctness Score: [[0.6]]

Note that each one of the golden answers is considered correct. Thus if the model's answer matches any one of the golden answers, it should be considered correct. Judge the below case, give the brief reasoning process and the correctness score.

Question: {prompt}
Golden Answer(s): {gold_ans}
Model's Answer: {response}
Your Judgment: 
""",
    },
]

image2text_gpt_judge_for_closeended_multiplechoice = lambda prompt, options, response: [
    {"role": "system", "content": f"In this task, I want you to act as an option extractor."},
    {
        "role": "user",
        "content": f"""You will be provided with a multiple-choice question, its options, and the model's answer, while the context of the question, which is one or more images, is not given here. Your task is to extract or judge which option is chosen by the model based on its response, without seeing the context of the question. The extracted option should be one of the provided option letters. Your should first briefly give your reasoning process, and then give the extracted option letter. The extracted option must strictly follow this format: \"[[option letter]]\", e.g., \"The option chosen by the model: [[A]]\".
Below are some examples. 

Example 1: 
Question: Where are the cast of the television show located in the image?
Options:
A. In the foreground
B. In the background
C. In the center
D. At the edges
Model's Answer: C. In the center
Your Judgment: The model's answer clearly states "C. In the center", indicating that the correct option, according to the model, is in the center. The option chosen by the model: [[C]].

Example 2: 
Question: <image_1> on the left was painted during the 
Options:
A. first or second century C. E.
B. sixth or seventh century C. E.
C. tenth or eleventh century C.E.
D. fourteenth or fifteenth century C. E.
Model's Answer: The correct answer is option D, the fourteenth or fifteenth century C.E.
Your Judgment: The model's response specifies "option D, the fourteenth or fifteenth century C.E." directly as the correct answer. The option chosen by the model: [[D]].   

Example 3: 
Question: what does the diagram show's you information about
Options:
A. Photosynthesis
B. The plant getting fed
C. A picture of the plant
D. What happens to a plant daily
Model's Answer: The diagram shows the process of photosynthesis, which is the process by which plants convert sunlight, carbon dioxide, and water into oxygen and glucose. 
Your Judgment: The model's answer mentions "the process of photosynthesis," which directly corresponds to option A, "Photosynthesis". Therefore, the correct option according to the model is photosynthesis.  The option chosen by the model: [[A]].

Give the brief reasoning process and the extracted option for the below case:

Question: {prompt}
Options: 
{options}
Model's Answer: {response}
Your Judgment: 
""",
    },
]


def get_score_from_judge(judge_response):
    """
    Get the score from the judge response.
    """
    one_score_pattern = re.compile("\[\[(\d+\.?\d*)\]\]")
    one_score_pattern_backup = re.compile("\[(\d+\.?\d*)\]")

    match = re.search(one_score_pattern, judge_response)
    if not match:
        match = re.search(one_score_pattern_backup, judge_response)

    if match:
        rating = ast.literal_eval(match.groups()[0])
    else:
        rating = round(random.random(), 1)

    return float(rating)


def get_eval(question, model_response: str, ground_truth: str, max_tokens: int, retries: int = 5):
    global client
    messages = image2text_gpt_judge_for_closeended_freeform(prompt=question, gold_ans=ground_truth, response=model_response)

    payload = {
        "model": MODEL_VERSION,
        "messages": messages,
        # "temperature": 0.2,
        "max_tokens": max_tokens,
    }

    for attempt in range(retries):
        try:
            # response = requests.post(API_URL, headers=headers, json=payload, timeout=60)
            response = client.chat.completions.create(**payload)
            # response.raise_for_status()
            response_data = response.json()

            # content = response_data["choices"][0]["message"]["content"].strip()
            content = response.choices[0].message.content.strip()
            if content != "":
                return content
            break  # If successful, break out of the loop

        except Exception as e:
            eval_logger.info(f"Attempt {attempt + 1} failed with error: {e}")
            if attempt < retries:  # If we have retries left, sleep and then continue to next attempt
                time.sleep(NUM_SECONDS_TO_SLEEP)
            else:  # If this was the last attempt, log and return empty
                eval_logger.error(f"All {retries} attempts failed. Last error message: {e}")
                return "[[0.0]]"
    return "[[0.0]]"


# A bit ugly here
# But the idea is that we will unzip all the zip files
# To HF HOME cache dir
# And load it here
HF_HOME = os.environ["HF_HOME"]
cache_dir = config["dataset_kwargs"]["cache_dir"]
cache_dir = os.path.join(HF_HOME, cache_dir)
cache_dir = os.path.join(cache_dir)


def mix_evals_image2text_doc_to_visual(doc):
    visual = []
    for image_path in doc["input_file"]:
        image_path = os.path.join(cache_dir, image_path)
        if os.path.exists(image_path):
            image_path = image_path

        visual.append(Image.open(image_path).convert("RGB"))

    return visual


# This is the place where you format your question
def mix_evals_image2text_doc_to_text(doc, lmms_eval_specific_kwargs=None):
    if lmms_eval_specific_kwargs is None:
        lmms_eval_specific_kwargs = {}
    pre_prompt = ""
    post_prompt = ""
    if "pre_prompt" in lmms_eval_specific_kwargs:
        pre_prompt = lmms_eval_specific_kwargs["pre_prompt"]
    if "post_prompt" in lmms_eval_specific_kwargs:
        post_prompt = lmms_eval_specific_kwargs["post_prompt"]

    user_prompt = doc["query"]

    if "options" in doc and len(doc["options"]) > 1:
        option_prompt = "Here are the options:\n"
        for idx, option in enumerate(doc["options"]):
            char_idx = chr(ord("A") + idx)
            option = option.strip()
            option_prompt += f"{char_idx}. {option}\n"

        option_prompt = option_prompt.rstrip("\n")
        user_prompt = f"{user_prompt}\n{option_prompt}"

    if pre_prompt:
        user_prompt = f"{pre_prompt}\n{user_prompt}"

    if post_prompt:
        user_prompt = f"{user_prompt}\n{post_prompt}"
    return user_prompt


OPEN_CONVS_PROMPT = """{PRE}
{FIRST}
{POST}
"""


def mix_evals_image2text_doc_to_text_open_convs(doc, lmms_eval_specific_kwargs=None):
    if lmms_eval_specific_kwargs is None:
        lmms_eval_specific_kwargs = {}
    pre_prompt = ""
    post_prompt = ""
    if "pre_prompt" in lmms_eval_specific_kwargs:
        pre_prompt = lmms_eval_specific_kwargs["pre_prompt"]
    if "post_prompt" in lmms_eval_specific_kwargs:
        post_prompt = lmms_eval_specific_kwargs["post_prompt"]

    filtered_first_turn = re.sub(r"<video_[0-9]+>", "", doc["first_turn_user_prompt"])
    return OPEN_CONVS_PROMPT.format(
        PRE=pre_prompt,
        POST=post_prompt,
        FIRST=filtered_first_turn,
    )


MODEL_CONVS_PROMPT = """{FIRST}
{MODEL_RESPONSE}
{PRE}
{SECOND}
{POST}
"""


def mix_evals_image2text_doc_to_text_open_2nd_convs(doc, lmms_eval_specific_kwargs=None):
    if lmms_eval_specific_kwargs is None:
        lmms_eval_specific_kwargs = {}
    pre_prompt = ""
    post_prompt = ""
    if "pre_prompt" in lmms_eval_specific_kwargs:
        pre_prompt = lmms_eval_specific_kwargs["pre_prompt"]
    if "post_prompt" in lmms_eval_specific_kwargs:
        post_prompt = lmms_eval_specific_kwargs["post_prompt"]

    return MODEL_CONVS_PROMPT.format(
        PRE=pre_prompt,
        POST=post_prompt,
        FIRST=doc["first_turn_user_prompt"],
        SECOND=doc["second_turn_user_prompt"],
        MODEL_RESPONSE=doc["model_response"],
    )


def mix_evals_image2text_process_results_open_convs(doc, result):
    pred = result[0]
    return {"submission": {"pred": pred, "question_idx": doc["question_index"], "first_turn_video_caption": doc["first_turn_video_caption"], "target": ""}}


def mix_evals_image2text_process_results_freeform(doc, result):
    pred = result[0]
    ground_truth_str = ", ".join([f'"{gt}"' for gt in doc["reference_answer"]])
    ground_truth_str = f"[{ground_truth_str}]"
    content = image2text_gpt_judge_for_closeended_freeform(response=pred, gold_ans=ground_truth_str, prompt=doc["query"])
    eval_answer = get_eval(model_response=pred, ground_truth=ground_truth_str, max_tokens=MAX_NEW_TOKENS, question=doc["query"])
    return {
        "submission": {"pred": pred, "question_idx": doc["id"], "target": doc["reference_answer"], "eval_answer": eval_answer, "gpt_prompt": content},
        "gpt_eval": {"pred": pred, "question_idx": doc["id"], "target": doc["reference_answer"], "eval_answer": eval_answer, "gpt_prompt": content},
    }


def mix_evals_image2text_aggregate_submissions(results, args, task):
    now_date_time = datetime.datetime.now().strftime("%Y-%m-%d-%H-%M-%S")
    submission_file_name = f"mix_evals_image2text_{task}-{now_date_time}.json"
    path = file_utils.generate_submission_file(submission_file_name, args)
    with open(path, "w") as f:
        json.dump(results, f)
    eval_logger.info(f"Submission file saved to {path}")


def mix_evals_image2text_gpt_eval(results, args):
    score = 0
    for result in results:
        eval_answer = result["eval_answer"]
        eval_score = get_score_from_judge(eval_answer)
        score += eval_score

    return score / len(results)


# Factory into different aggregate
def mix_evals_image2text_aggregate_gen(results, args):
    mix_evals_image2text_aggregate_submissions(results, args, "OpenConvs")


class GPTMultiChoiceFilter(Filter):
    def __init__(self, gpt_version: str = "gpt-3.5-turbo-0125", retries: int = 5):
        """
        Can define custom behavior here, if an individual instantiation of a Filter class should have state.
        """
        self.gpt_version = gpt_version

        if API_TYPE == "openai":
            self.client = openai.OpenAI(api_key=API_KEY)
        elif API_TYPE == "azure":
            self.client = openai.AzureOpenAI(api_key=API_KEY, azure_endpoint=API_URL)

        self.retries = retries

    def apply(self, resps, docs):
        """
        Defines the operation to perform on a list of the `inst.resps` properties of `Instance` objects.
        Should return the list of (filtered) response lists *in the same order as they were input*, e.g.
        if pass in [<inst.resps for instance 0>, <inst.resps for instance 1>] should return
        [<filtered resps for instance 0>, <filtered resps for instance 1>]
        """
        results = []
        for response, doc in zip(resps, docs):
            query = doc["query"]
            options = "\n".join([f"{chr(ord('A') + idx)}. {option}" for idx, option in enumerate(doc["options"])])
            message = image2text_gpt_judge_for_closeended_multiplechoice(prompt=query, options=options, response=response)
            payload = {
                "model": self.gpt_version,
                "messages": message,
                "max_tokens": MAX_NEW_TOKENS,
            }
            result = 0
            for attempt in range(self.retries):
                try:
                    # response = requests.post(API_URL, headers=headers, json=payload, timeout=60)
                    # print(payload)
                    response = client.chat.completions.create(**payload)
                    # print(response)
                    # response.raise_for_status()

                    # content =["choices"][0]["message"]["content"].strip()
                    content = response.choices[0].message.content
                    # print("content:", content)
                    if content:
                        match = re.search(r"\[\[([A-Z])\]\]", content)
                        # print("match:", match)
                        if not match:
                            match = re.search(r"r'\b([A-Z])\.?\b'", content)
                            # print("match:", match)
                        if match:
                            # print("=====")
                            # print(match.group(1))
                            result = ord(match.group(1)) - ord("A")
                            # print("result:", result)
                            # print("=====")
                            # print(content, result)
                        else:
                            result = 0
                    break  # If successful, break out of the loop

                except Exception as e:
                    eval_logger.info(f"Attempt {attempt + 1} failed with error: {e}")
                    import traceback

                    print(traceback.format_exc())
                    if attempt < self.retries:  # If we have retries left, sleep and then continue to next attempt
                        time.sleep(NUM_SECONDS_TO_SLEEP)
                    else:  # If this was the last attempt, log and return empty
                        eval_logger.error(f"All {self.retries} attempts failed. Last error message: {e}")
                        result = 0
                        break
            results.append(str(result))
        return results
