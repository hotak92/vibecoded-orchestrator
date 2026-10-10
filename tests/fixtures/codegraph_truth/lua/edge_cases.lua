-- Edge cases for the definitions gate (Lua).
local Stack = {}
Stack.__index = Stack

function Stack.new()
  return setmetatable({ items = {} }, Stack)
end

function Stack:push(v)
  table.insert(self.items, v)
end

Stack.pop = function(self)
  return table.remove(self.items)
end

local function helper(x)
  local function inner(y)
    return y * 2
  end
  return inner(x)
end

local format = function(s)
  return "[" .. s .. "]"
end

utils = {}
function utils.trim(s)
  return (s:gsub("^%s+", ""))
end

local label = "function fake() end"
