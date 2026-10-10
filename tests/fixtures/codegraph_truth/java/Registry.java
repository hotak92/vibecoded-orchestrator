// Edge cases for the definitions gate (Java).
package edge;

import java.util.List;

@SuppressWarnings("unchecked")
public final class Registry<T extends Comparable<T>> {
    private final List<T> items;

    public Registry(List<T> items) {
        this.items = items;
    }

    @Override
    public String toString() {
        return "Registry";
    }

    public <R> List<R> map(java.util.function.Function<T, R> f) {
        return null;
    }

    static class Node {
        int value;

        Node next() {
            return null;
        }
    }

    enum Color { RED, GREEN }

    Runnable task = new Runnable() {
        public void run() {
            String s = "void fake() {}";
        }
    };
}

record Point(int x, int y) {
    int sum() {
        return x + y;
    }
}
