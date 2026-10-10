// Geometry module for the golden-fixture repo (C++, regex-parsed).
// Exercises: namespace, class + struct, templates, out-of-line methods.

#include <vector>
#include <string>
#include <cmath>

namespace shapes {

class Circle {
public:
    Circle(double radius);
    double area() const;
    double circumference() const;

private:
    double radius_;
};

struct Point {
    double x;
    double y;
};

template <typename T>
class Box {
public:
    T value;
};

}  // namespace shapes

// Out-of-line method definitions: ClassName::method(...) — the only
// method shape the regex extractor captures.
shapes::Circle::Circle(double radius) : radius_(radius) {}

double shapes::Circle::area() const {
    return 3.14159 * radius_ * radius_;
}

double shapes::Circle::circumference() const {
    return 2.0 * 3.14159 * radius_;
}

double distance(const shapes::Point& a, const shapes::Point& b) {
    double dx = a.x - b.x;
    double dy = a.y - b.y;
    return std::sqrt(dx * dx + dy * dy);
}

namespace shapes {

// v0.2.92 WP-5c: free functions at NAMESPACE scope — INDENTED, which is
// what a column-0 anchor misses, and the commonest shape in real C++.
int quadrant(const Point& p) {
    if (p.x >= 0.0) {
        return p.y >= 0.0 ? 1 : 4;
    }
    return p.y >= 0.0 ? 2 : 3;
}

// A `template<...>` clause on its own line belongs to the declaration.
template <typename T>
T smaller(T a, T b) {
    return a < b ? a : b;
}

}  // namespace shapes

// A header-only type: both members are DEFINED in the class body, so neither
// has a `Class::` for `method_pattern` to anchor on and both are attributed
// to `Tally` by containment in its line range.
class Tally {
public:
    void add(double v) { total_ += v; }

    double total() const {
        return total_;
    }

private:
    double total_ = 0.0;
};

// NEGATIVE SPACE carried by the corpus itself: a lambda, a brace-initializer
// list and two control-flow headers. None of them may mint a row, and this
// file is where a regression in those guards becomes a snapshot diff.
double summarize(const std::vector<shapes::Point>& pts) {
    auto axis = [](const shapes::Point& p) { return p.x; };
    std::vector<double> seeds = {0.0, 1.0};
    Tally tally;
    if (pts.empty()) {
        return seeds.front();
    }
    for (const shapes::Point& p : pts) {
        tally.add(axis(p));
    }
    return tally.total();
}
