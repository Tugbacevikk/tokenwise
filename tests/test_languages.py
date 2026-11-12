from tokenwise.focus import focused_view
from tokenwise.repo_map import outline, outline_js


def test_ts_class_methods_but_not_control_flow():
    source = (
        "export class Cart {\n"
        "  private items: string[] = [];\n"
        "  constructor(private api: Api) {}\n"
        "  async add(item: string): Promise<void> {\n"
        "    if (item) { this.items.push(item); }\n"
        "    for (const x of this.items) { log(x); }\n"
        "  }\n"
        "  get size(): number {\n    return this.items.length;\n  }\n"
        "}\n"
    )
    assert outline_js(source) == ["class Cart", "  add(item: string)", "  size()"]


def test_java():
    source = (
        "package shop;\n\npublic class Cart {\n"
        "    private final List<String> items = new ArrayList<>();\n"
        "    public void add(String item) {\n        items.add(item);\n    }\n"
        "    public static Map<String, Integer> count(List<String> xs) {\n        return null;\n    }\n"
        "}\n"
        "interface Store {}\n"
    )
    assert outline("Cart.java", source) == [
        "class Cart",
        "  add(String item)",
        "  count(List<String> xs)",
        "interface Store",
    ]


def test_csharp_kotlin_php():
    cs = "public sealed class Cart {\n    public async Task<int> Total(int tax) {\n        return 0;\n    }\n}\n"
    assert outline("Cart.cs", cs) == ["class Cart", "  Total(int tax)"]
    kt = "data class Item(val id: Int)\n\nclass Cart {\n    suspend fun load(id: Int): Item {\n        TODO()\n    }\n}\nfun List<Item>.total() = size\n"
    assert outline("Cart.kt", kt) == [
        "class Item",
        "class Cart",
        "fun load(id: Int)",
        "fun total()",
    ]
    php = "<?php\nclass Cart {\n    public function add($item) {\n    }\n}\nfunction helper(int $x) {}\n"
    assert outline("cart.php", php) == [
        "class Cart",
        "function add($item)",
        "function helper(int $x)",
    ]


def test_go_and_rust():
    go = "package shop\n\ntype Cart struct {\n\tItems []string\n}\n\nfunc (c *Cart) Add(item string) {\n}\n\nfunc New() *Cart {\n\treturn &Cart{}\n}\n"
    assert outline("cart.go", go) == [
        "type Cart struct",
        "func (c *Cart) Add(item string)",
        "func New()",
    ]
    rs = "pub struct Cart {\n    items: Vec<String>,\n}\n\nimpl Cart {\n    pub fn add(&mut self, item: String) {\n    }\n}\n\npub trait Store {}\n"
    assert outline("cart.rs", rs) == [
        "struct Cart",
        "impl Cart",
        "fn add(&mut self, item: String)",
        "trait Store",
    ]


def test_focus_works_for_go():
    funcs = "".join(f"func F{i}(x int) int {{\n\treturn x + {i}\n}}\n\n" for i in range(40))
    go = 'package shop\n\nimport "fmt"\n\n' + funcs + "func Target() {\n\tfmt.Println(1)\n}\n"
    view = focused_view("x.go", go, ["Target"])
    assert view.startswith('package shop\n\nimport "fmt"\n')
    assert "func Target() {\n\tfmt.Println(1)\n}\n" in view
    assert "F7(x int)" not in view.split("Everything defined")[0]
