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
