import re
import json

# read the JS file
with open("local_pool.js", "r", encoding="utf-8") as f:
    js = f.read()

# find the LOCAL_POOL builder section
start = js.find("function buildLocalPool()")
end = js.find("return pool;")

code = js[start:end]

# remove function wrappers to extract question objects
objects = re.findall(r'add\(\{([\s\S]*?)\}\);', code)

questions = []

for obj in objects:
    # convert JS object style to JSON style
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

# write JSON
with open("questions.json", "w", encoding="utf-8") as f:
    json.dump(questions, f, indent=2)

print("questions.json created with", len(questions), "questions")