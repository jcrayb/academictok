## MOTIVATION

I am a PhD student in Operations Research. I am having trouble going out into the ether and finding academic papers to read, both because it is tough and also because I do find it decently boring. I have an idea, inspired by someone who made a doomscrolling version of wikipedia. I would like to make a website like Reddit, but instead each subreddit represents a field, and contains posts which are key summaries of papers in that field. I have a Semantic Scholar API key, which can get relevant papers based on a query, and other functions to find and filter academic papers. I would like to make this into a website with a Python backend, and an Astro.js frontend.

## BACKEND

- Query the API (rate limit of 1 query per second, per the API rules), find important and relevant papers
- Use a local LLM via Ollama to create a summary of the paper.
- Caching to avoid having to query every page load.

## FRONTEND/UX

- Subreddit = one field (stochastic optimization, metal additive manufacturing, etc.)
- Each post  = 1 paper, with summary title and summary text
- Main screen has two modes, scrolling through only subscribed field, or scrolling a main feed that includes other fields not subscribed to

## ORGANIZATION DETAILS

The python API should be in the main dir, while the astro frontend should be in a subdir named `frontend`. The python API should use Flask, and live on port 8080. The frontend should use Astro, and be deployed on port 4321
