import json
import re

# read the JS pool
with open("local_pool.js", "r", encoding="utf-8") as f:
    js = f.read()

# extract question objects between { and }
objects = re.findall(r'add\(\{([\s\S]*?)\}\);', js)

questions = []

for obj in objects:
    text = obj

    text = text.replace("type:", '"type":')
    text = text.replace("topic:", '"topic":')
    text = text.replace("difficulty:", '"difficulty":')
    text = text.replace("marks:", '"marks":')
    text = text.replace("question:", '"question":')
    text = text.replace("options:", '"options":')
    text = text.replace("answer:", '"answer":')
    text = text.replace("solution:", '"solution":')
    text = text.replace("tolerance:", '"tolerance":')
    text = text.replace("decimals:", '"decimals":')

    text = re.sub(r'id\s*:\s*([a-zA-Z0-9_]+)', '"id": "\\1"', text)

    try:
        q = json.loads("{" + text + "}")
        questions.append(q)
    except:
        pass

with open("questions.json", "w", encoding="utf-8") as f:
    json.dump(questions, f, indent=2)

print("questions.json created with", len(questions), "questions")