# Embeddings and Vector Search

An embedding is a list of numbers, called a vector, that represents the meaning
of a piece of text. Texts with similar meaning produce vectors that point in
similar directions, even when they share no words. This is what makes semantic
search possible: you can find passages about a topic without matching exact
keywords.

Similarity between two vectors is usually measured with cosine similarity, which
is the cosine of the angle between them. A score near 1.0 means very similar; a
score near 0 means unrelated. To search, you embed the query, compute its cosine
similarity against every stored vector, and keep the highest-scoring ones.

Gemini's embedding model, gemini-embedding-001, supports task types. Documents
should be embedded with the RETRIEVAL_DOCUMENT task type and queries with the
RETRIEVAL_QUERY task type. Matching the task type to the role of the text
improves retrieval accuracy. The model outputs 3072 dimensions by default but
can be truncated to smaller sizes such as 768 to save storage.

For small projects an in-memory list of vectors is enough. At larger scale a
dedicated vector database such as Chroma, FAISS, or pgvector handles storage and
fast approximate search.
