//! A deliberately tiny library for trying the AI Software Factory.

/// Returns a greeting for `name`, or for the world when `name` is empty.
pub fn greet(name: &str) -> String {
    if name.is_empty() {
        return "Hello, World!".to_string();
    }
    format!("Hello, {name}!")
}
