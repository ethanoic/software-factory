package com.example;

import static org.junit.jupiter.api.Assertions.assertEquals;

import org.junit.jupiter.api.Test;

class GreeterTest {
    @Test
    void greetsTheWorldByDefault() {
        assertEquals("Hello, World!", new Greeter().greet(""));
    }

    @Test
    void greetsByName() {
        assertEquals("Hello, Ada!", new Greeter().greet("Ada"));
    }
}
