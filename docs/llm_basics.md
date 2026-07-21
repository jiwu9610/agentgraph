# Working with Language Model APIs

A language model API takes text in and returns text out. The most important
thing to understand is that the API is stateless: the model does not remember
previous calls. To hold a conversation, you must send the entire history back on
every request. Helper libraries hide this by keeping the history in a list for
you, but the underlying calls always include the full transcript.

A system instruction is a special message that sets the model's behavior and
persona for the whole conversation. It is separate from the user's messages and
is a good place to put rules like "answer concisely" or "only use the provided
context."

Tokens are the unit language models read and write. A token is roughly a few
characters. Both pricing and context limits are measured in tokens, so counting
tokens before sending a request is how you predict cost and avoid exceeding the
model's context window.

Two features help control cost. Streaming returns the response in pieces as it
is generated, improving perceived speed. Context caching stores a large, reused
prefix such as a long system prompt or document set so it is not reprocessed and
recharged on every request.

Structured output constrains the model to return JSON matching a schema you
define, so the result can be used directly by code instead of being parsed from
free text.
