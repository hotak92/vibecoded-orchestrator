-- Vector module for the golden-fixture repo (Lua, regex-parsed).
-- Exercises: table-based class (Name = {} + __index), colon + dot
-- methods, a standalone function, an assigned-function, and nested
-- `if/for ... end` blocks (the tricky multi-`end` case).

local mathlib = require('mathlib')

Vector = {}
Vector.__index = Vector

function Vector.new(x, y)
    local self = setmetatable({}, Vector)
    self.x = x
    self.y = y
    return self
end

function Vector:magnitude()
    return math.sqrt(self.x * self.x + self.y * self.y)
end

Vector.scale = function(self, factor)
    self.x = self.x * factor
    self.y = self.y * factor
    return self
end

-- Standalone function with nested end-keyword blocks.
function clamp(value, lo, hi)
    if value < lo then
        for _ = 1, 1 do
            value = lo
        end
    elseif value > hi then
        value = hi
    end
    return value
end
