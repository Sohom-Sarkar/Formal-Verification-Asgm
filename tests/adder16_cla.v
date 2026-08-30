// Revision: same function, completely different topology - four 4-bit
// carry-lookahead blocks chained together. Logarithmic carry within each block.
module cla4(input [3:0] a, input [3:0] b, input cin,
            output [3:0] s, output cout);
  wire [3:0] g, p;
  wire [4:0] c;

  assign g = a & b;          // generate
  assign p = a ^ b;          // propagate

  assign c[0] = cin;
  assign c[1] = g[0] | (p[0] & c[0]);
  assign c[2] = g[1] | (p[1] & g[0]) | (p[1] & p[0] & c[0]);
  assign c[3] = g[2] | (p[2] & g[1]) | (p[2] & p[1] & g[0])
              | (p[2] & p[1] & p[0] & c[0]);
  assign c[4] = g[3] | (p[3] & g[2]) | (p[3] & p[2] & g[1])
              | (p[3] & p[2] & p[1] & g[0])
              | (p[3] & p[2] & p[1] & p[0] & c[0]);

  assign s    = p ^ c[3:0];
  assign cout = c[4];
endmodule

module adder16(input [15:0] a, input [15:0] b, input cin,
               output [15:0] sum, output cout);
  wire k1, k2, k3;
  cla4 u0(.a(a[3:0]),   .b(b[3:0]),   .cin(cin), .s(sum[3:0]),   .cout(k1));
  cla4 u1(.a(a[7:4]),   .b(b[7:4]),   .cin(k1),  .s(sum[7:4]),   .cout(k2));
  cla4 u2(.a(a[11:8]),  .b(b[11:8]),  .cin(k2),  .s(sum[11:8]),  .cout(k3));
  cla4 u3(.a(a[15:12]), .b(b[15:12]), .cin(k3),  .s(sum[15:12]), .cout(cout));
endmodule
