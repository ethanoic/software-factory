use hello_factory::greet;

#[test]
fn greets_the_world_by_default() {
    assert_eq!(greet(""), "Hello, World!");
}

#[test]
fn greets_by_name() {
    assert_eq!(greet("Ada"), "Hello, Ada!");
}
