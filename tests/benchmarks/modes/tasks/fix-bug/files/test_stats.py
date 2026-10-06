from stats import mean, median, spread

assert mean([2, 4, 6]) == 4, mean([2, 4, 6])
assert mean([5]) == 5
assert median([3, 1, 2]) == 2
assert median([4, 1, 3, 2]) == 2.5
assert spread([7, 2, 9]) == 7
print("all tests passed")
