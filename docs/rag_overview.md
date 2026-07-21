# Retrieval-Augmented Generation (RAG)

RAG is a technique for answering questions over a specific body of documents
that a language model was not trained on. Instead of relying on the model's
memory, you retrieve the most relevant passages from your own documents and
include them in the prompt, then ask the model to answer using only that text.

A RAG system has two phases. The ingestion phase runs offline: documents are
split into chunks, each chunk is converted into an embedding vector, and the
vectors are stored. The query phase runs per question: the question is embedded,
the most similar chunks are retrieved by comparing vectors, and those chunks are
placed into the prompt as context.

The main benefit of RAG is grounding. Because the model answers from supplied
text, it is far less likely to hallucinate, and it can cite its sources. RAG
also lets a small, cheap model answer questions about private or up-to-date data
without any retraining.

A common failure mode is poor retrieval: if the wrong chunks are fetched, even a
strong model gives a wrong answer. This is why evaluating retrieval quality
separately from answer quality matters.
