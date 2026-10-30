#include "src/RefreshPolicy.h"
#include <cassert>
#include <limits>

int main() {
    assert(supportsInput120({72, 90, 120}));
    assert(supportsInput120({120}));
    assert(!supportsInput120({72, 80, 90}));
    assert(!supportsInput120({60, 144}));
    assert(!supportsInput120({}));
    assert(!supportsInput120({std::numeric_limits<float>::quiet_NaN(),
                             std::numeric_limits<float>::infinity()}));
    for (unsigned bits = 0; bits < 8; ++bits)
        assert(inputButtons(bits & 1, bits & 2, bits & 4) == bits);
}
