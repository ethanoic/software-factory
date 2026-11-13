class_name Greeter
extends RefCounted

## Returns a greeting for name, or for the world when name is empty.
func greet(name: String = "") -> String:
	if name.is_empty():
		return "Hello, World!"
	return "Hello, %s!" % name
