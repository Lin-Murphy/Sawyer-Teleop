#pragma once
#include <array>
#include <cstdint>
#include <string>
#include <vector>

// Small dependency-free bitmap font. HUD text uses ASCII so no system fonts are required.
namespace StatusPanel {
constexpr int width = 640;
constexpr int height = 360;
using Color = std::array<uint8_t, 4>;
const Color white{230, 240, 245, 255};
const Color green{90, 240, 160, 255};
const Color amber{255, 195, 80, 255};
const Color muted{155, 175, 190, 255};
inline std::array<uint8_t, 7> glyph(char c) {
    switch (c) {
    case 'A': return {14,17,17,31,17,17,17};
    case 'B': return {30,17,17,30,17,17,30};
    case 'C': return {14,17,16,16,16,17,14};
    case 'D': return {30,17,17,17,17,17,30};
    case 'E': return {31,16,16,30,16,16,31};
    case 'F': return {31,16,16,30,16,16,16};
    case 'G': return {14,17,16,23,17,17,15};
    case 'H': return {17,17,17,31,17,17,17};
    case 'I': return {14,4,4,4,4,4,14};
    case 'J': return {7,2,2,2,18,18,12};
    case 'K': return {17,18,20,24,20,18,17};
    case 'L': return {16,16,16,16,16,16,31};
    case 'M': return {17,27,21,21,17,17,17};
    case 'N': return {17,25,21,19,17,17,17};
    case 'O': return {14,17,17,17,17,17,14};
    case 'P': return {30,17,17,30,16,16,16};
    case 'Q': return {14,17,17,17,21,18,13};
    case 'R': return {30,17,17,30,20,18,17};
    case 'S': return {15,16,16,14,1,1,30};
    case 'T': return {31,4,4,4,4,4,4};
    case 'U': return {17,17,17,17,17,17,14};
    case 'V': return {17,17,17,17,17,10,4};
    case 'W': return {17,17,17,21,21,21,10};
    case 'X': return {17,17,10,4,10,17,17};
    case 'Y': return {17,17,10,4,4,4,4};
    case 'Z': return {31,1,2,4,8,16,31};
    case '0': return {14,17,19,21,25,17,14};
    case '1': return {4,12,4,4,4,4,14};
    case '2': return {14,17,1,2,4,8,31};
    case '3': return {30,1,1,14,1,1,30};
    case '4': return {2,6,10,18,31,2,2};
    case '5': return {31,16,16,30,1,1,30};
    case '6': return {14,16,16,30,17,17,14};
    case '7': return {31,1,2,4,8,8,8};
    case '8': return {14,17,17,14,17,17,14};
    case '9': return {14,17,17,15,1,1,14};
    case ':': return {0,4,4,0,4,4,0};
    case '.': return {0,0,0,0,0,6,6};
    case '-': return {0,0,0,31,0,0,0};
    case '/': return {1,2,2,4,8,8,16};
    case '+': return {0,4,4,31,4,4,0};
    default: return {};
    }
}
struct Line { std::string text; Color color = white; };
inline std::vector<uint8_t> render(const std::vector<Line>& lines) {
    std::vector<uint8_t> pixels(width * height * 4);
    auto put = [&](int x, int y, Color color) {
        if (x < 0 || x >= width || y < 0 || y >= height) return;
        // OpenGL texture origin is bottom-left; text coordinates are top-left.
        int index = ((height - 1 - y) * width + x) * 4;
        for (int c = 0; c < 4; ++c) pixels[index + c] = color[c];
    };
    for (int y = 0; y < height; ++y)
        for (int x = 0; x < width; ++x)
            put(x, y, {10, 18, 24, 215}); // premultiplied translucent dark background
    int y = 18;
    for (const auto& line : lines) {
        if (y + 21 > height - 10) break;
        int x = 18;
        for (char c : line.text) {
            if (x + 15 > width - 12) break;
            auto bits = glyph(c);
            for (int row = 0; row < 7; ++row)
                for (int col = 0; col < 5; ++col)
                    if (bits[row] & (1 << (4 - col)))
                        for (int dy = 0; dy < 3; ++dy)
                            for (int dx = 0; dx < 3; ++dx)
                                put(x + col * 3 + dx, y + row * 3 + dy, line.color);
            x += 18;
        }
        y += 32;
    }
    return pixels;
}
} // namespace StatusPanel
