"""Domain events and the in-process event bus.

Events decouple "something happened" from "these things must react to it". The
publisher names a fact; subscribers register interest. Adding a reaction becomes
a new subscriber rather than an edit to the publishing feature.

``base`` defines the event primitives; ``bus`` routes them. Feature-specific
event classes live in the module that publishes them, not here.
"""
