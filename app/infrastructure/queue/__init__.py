"""Background job queue.

Defines how work is deferred out of the request cycle: the job model, the
producer contract and the task registry that maps job names to handlers.

Anything slow, retryable or dependent on a third party belongs here.
"""
