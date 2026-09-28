import tempfile
import unittest
from pathlib import Path
from crawler import Store, export, highlights, parse_detail


def fixture(ctr="CTR20260001", status="进行中 （招募中）"):
    return f'''<html><body><table>
    <tr><td>登记号</td><td>{ctr}</td></tr>
    <tr><td>试验专业题目</td><td>合成测试项目</td></tr>
    <tr><td>入选标准</td><td><table>
    <tr><td>1</td><td>年龄≥18岁</td></tr>
    <tr><td>2</td><td>既往治疗失败，允许队列A例外</td></tr>
    </table></td></tr>
    <tr><td>排除标准</td><td><table><tr><td>1</td><td>活动性感染</td></tr></table></td></tr>
    <tr><td>联系人姓名</td><td>申请人联系人甲</td><td>联系人座机</td><td>000-0000</td></tr>
    </table><h3>1、主要研究者信息</h3><table>
    <tr><td>姓名</td><td>研究者甲</td></tr>
    <tr><td>电话</td><td>111</td><td>Email</td><td>a@example.invalid</td></tr>
    <tr><td>单位名称</td><td>机构甲</td></tr>
    <tr><td>姓名</td><td>研究者乙</td></tr>
    <tr><td>电话</td><td>222</td><td>Email</td><td>b@example.invalid</td></tr>
    <tr><td>单位名称</td><td>机构乙</td></tr>
    </table><h3>2、各参加机构信息</h3><p>不应提取的机构联系人</p>
    <h3>六、试验状态信息</h3><p>{status}</p></body></html>'''


class CollectorTests(unittest.TestCase):
    def test_nested_criteria_and_contact_roles(self):
        d = parse_detail(fixture(), "CTR20260001")
        self.assertEqual(d["入选标准"], ["年龄≥18岁", "既往治疗失败，允许队列A例外"])
        self.assertEqual(d["联系人姓名"], "申请人联系人甲")
        self.assertEqual([p["电话"] for p in d["主要研究者"]], ["111", "222"])
        self.assertEqual(d["主要研究者"][1]["单位名称"], "机构乙")

    def test_wrong_or_incomplete_page_fails(self):
        with self.assertRaises(ValueError):
            parse_detail(fixture(), "CTR20260002")
        with self.assertRaises(ValueError):
            parse_detail(fixture().replace("排除标准", "未加载"), "CTR20260001")

    def test_key_excerpt_preserves_exceptions(self):
        self.assertIn("允许队列A例外", highlights(["既往治疗失败，允许队列A例外"]))

    def test_missing_phone_does_not_shift_email(self):
        d = parse_detail(fixture().replace("<td>111</td>", "<td></td>"), "CTR20260001")
        self.assertEqual(d["主要研究者"][0]["电话"], "页面未公开")
        self.assertEqual(d["主要研究者"][0]["Email"], "a@example.invalid")

    def test_dedupe_resume_and_export_status_separation(self):
        from openpyxl import load_workbook
        with tempfile.TemporaryDirectory() as folder:
            s = Store(folder)
            for key in ["瘤", "癌"]:
                s.add(key, ["1", "CTR20260001", "进行中", "药物", "适应症", "项目"])
            s.save("CTR20260001", parse_detail(fixture(), "CTR20260001"), fixture())
            s.add("瘤", ["2", "CTR20260002", "进行中", "药物", "适应症", "项目"])
            s.save("CTR20260002", parse_detail(fixture("CTR20260002", "已完成"), "CTR20260002"), fixture("CTR20260002", "已完成"))
            s.add("瘤", ["3", "CTR20260003", "进行中", "药物", "适应症", "项目"])
            s.fail("CTR20260003", "测试失败")
            self.assertTrue(s.done("CTR20260001"))
            export(s)
            book = load_workbook(Path(folder) / "肿瘤项目.xlsx")
            self.assertEqual(book["进行中项目"].max_row, 2)
            self.assertEqual(book["状态已变更"].max_row, 2)
            self.assertEqual(book["待补采"].max_row, 2)
            self.assertEqual(book["入排全文"].max_row, 4)
            self.assertEqual(book["进行中项目"]["G2"].value, "111\n222")
            book.close()
            s.db.close()


if __name__ == "__main__":
    unittest.main()
