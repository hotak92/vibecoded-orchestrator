// Inventory module for the golden-fixture repo (C#, regex-parsed).
// Exercises: namespace, interface, class, record, generic method,
// properties (included in the methods list), and ASP.NET route attributes.

using System;
using System.Collections.Generic;

namespace Warehouse
{
    public interface IRepository
    {
        Item Find(int id);
    }

    public record Item(int Id, string Name);

    [Route("api/items")]
    public class InventoryController
    {
        public int Count { get; set; }

        public Item Lookup(int id)
        {
            return new Item(id, "widget");
        }

        public List<T> WrapAll<T>(T single)
        {
            return new List<T> { single };
        }

        [HttpGet("all")]
        public List<Item> GetAll()
        {
            return new List<Item>();
        }

        [HttpPost("add")]
        public Item Add(Item item)
        {
            return item;
        }
    }
}
