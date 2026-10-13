// Edge cases for the definitions gate (C++).
#include <string>

namespace edge {

class Widget final : public Base {
public:
    Widget();
    ~Widget();
    int size() const { return 3; }
private:
    int n_;
};

Widget::Widget() : n_(0) {}

Widget::~Widget() {}

struct Config {
    std::string name;
};

template <typename T>
T twice(T v) {
    return v + v;
}

static int helper(int x) {
    auto f = [](int y) { return y; };
    return f(x);
}

}  // namespace edge

int main() {
    const char* s = "void fake() {}";
    return edge::helper(s[0]);
}
