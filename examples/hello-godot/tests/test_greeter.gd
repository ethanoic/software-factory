extends GutTest

func test_greets_the_world_by_default():
	assert_eq(Greeter.new().greet(), "Hello, World!")

func test_greets_by_name():
	assert_eq(Greeter.new().greet("Ada"), "Hello, Ada!")
