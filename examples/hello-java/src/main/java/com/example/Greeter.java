package com.example;

/** A deliberately tiny class for trying the AI Software Factory. */
public class Greeter {
    /** Returns a greeting for name, or for the world when name is empty. */
    public String greet(String name) {
        if (name == null || name.isEmpty()) {
            return "Hello, World!";
        }
        return "Hello, " + name + "!";
    }
}
