from dotenv import load_dotenv

# This must happen before importing ollama
load_dotenv()

import ollama

response = ollama.web_search("What is Ollama?")

for result in response.results:
    print(result.title)
    print(result.url)
    print(result.content)
    print()